#!/usr/bin/env python3
"""Tests for run-in-sandbox.py's result classification.

A stub openshell on PATH runs the remote wrapper locally (paths rewritten into
a scratch root, the exec --timeout enforced with `timeout`, the wrapper shell
being dash when the host has it, as the coder image does), so the cases that
matter are exercised without a gateway: output with and without a trailing
newline, status-like text in the output, a command that itself exits 124 or
125, a forged status line followed by a hang, a backgrounded writer that adds
a status line after the wrapper's, a CRLF status line from the command, a NUL
byte in the output, a large log, a command that kills the wrapper shell, an
exec that never runs the wrapper, an exec that outlives the host-side grace,
every preflight failure, a failed archive, upload or create, a sandbox that
never becomes Ready, a missing provider, a non-numeric timeout, and one or two
SIGTERMs while the command runs.
"""
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run-in-sandbox.py"

STUB_OPENSHELL = r'''#!/usr/bin/env bash
# Stub openshell. STUB_ROOT holds the fake sandbox filesystem; the knobs
# STUB_PROVIDER_RC, STUB_CREATE_RC, STUB_GET_RC, STUB_GET_PHASE, STUB_UPLOAD_RC,
# STUB_EXEC_RC, STUB_EXEC_NOSCRIPT, STUB_EXEC_IGNORE_TERM,
# STUB_EXEC_IGNORE_TIMEOUT, STUB_DRAIN_S and STUB_SH drive the cases.
case "$1 $2" in
  "provider get") exit "${STUB_PROVIDER_RC:-0}" ;;
  "sandbox create")
    printf '\nCreated sandbox: stub\n\n  [0.0s] Requesting compute...\n'
    exit "${STUB_CREATE_RC:-0}" ;;
  "sandbox get")
    [[ "${STUB_GET_RC:-0}" != 0 ]] && exit "$STUB_GET_RC"
    printf 'Sandbox:\n\n  Id: 0\n  Name: %s\n  Phase: %s\n  Conditions:\n    - Ready: True\n' \
      "$3" "${STUB_GET_PHASE:-Ready}"
    exit 0 ;;
  "sandbox upload")
    [[ "${STUB_UPLOAD_RC:-0}" != 0 ]] \
      && { echo "stub openshell: upload failed" >&2; exit "$STUB_UPLOAD_RC"; }
    mkdir -p "$STUB_ROOT/tmp" && cp "$4" "$STUB_ROOT/tmp/" && echo "Upload complete" ;;
  "sandbox delete") echo "$3" >> "$STUB_ROOT/deleted"; exit 0 ;;
  "sandbox exec")
    tmo=0; script=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --timeout) tmo="$2"; shift 2 ;;
        --) shift; script="${*: -1}"; break ;;
        *) shift ;;
      esac
    done
    # STUB_EXEC_IGNORE_TERM: a relay client that does not die on TERM.
    [[ "${STUB_EXEC_IGNORE_TERM:-0}" == 1 ]] && trap '' TERM
    # STUB_EXEC_NOSCRIPT: the exec never reaches the sandbox (relay error).
    [[ "${STUB_EXEC_NOSCRIPT:-0}" == 1 ]] && { echo "stub openshell: relay error" >&2; exit 1; }
    # The remote script addresses the sandbox's own paths; point them at the
    # scratch root. Everything else it needs (sh, bash, tar, printf) is on the
    # PATH it exports.
    script="${script//\/sandbox\/workspace\/regression/$STUB_ROOT/ws}"
    script="${script//\/tmp\/eval-reg-/$STUB_ROOT/tmp/eval-reg-}"
    # STUB_EXEC_IGNORE_TIMEOUT: a gateway that does not enforce --timeout, so
    # the host-side deadline is what ends the run.
    if [[ "${STUB_EXEC_IGNORE_TIMEOUT:-0}" == 1 ]]; then
      "${STUB_SH:-sh}" -c "$script" </dev/null; rc=$?
    else
      timeout "$tmo" "${STUB_SH:-sh}" -c "$script" </dev/null; rc=$?
    fi
    # STUB_DRAIN_S: like the supervisor, keep forwarding output for a moment
    # after the shell exits, so an orphaned writer's line lands in the log.
    [[ -n "${STUB_DRAIN_S:-}" ]] && sleep "$STUB_DRAIN_S"
    # STUB_EXEC_RC: the exec transport failed after the command ran (relay
    # dropped, sandbox evicted), whatever the command printed.
    exit "${STUB_EXEC_RC:-$rc}" ;;
  *) echo "stub openshell: unexpected args: $*" >&2; exit 2 ;;
esac
'''


class TestSandboxRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("timeout", "tar", "bash"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} not on PATH")
        if importlib.util.find_spec("yaml") is None and shutil.which("yq") is None:
            raise unittest.SkipTest("neither PyYAML nor yq is available")
        cls.tmp = Path(tempfile.mkdtemp())
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()
        (cls.bin / "openshell").write_text(STUB_OPENSHELL)
        (cls.bin / "openshell").chmod(0o755)
        cls.stub_root = cls.tmp / "stub"
        (cls.stub_root / "tmp").mkdir(parents=True)
        harness_dir = cls.tmp / "harness"
        harness_dir.mkdir()
        (harness_dir / "code.yaml").write_text("image: stub-image\npolicy: policies/base.yaml\n")
        (harness_dir / "no-image.yaml").write_text("policy: policies/base.yaml\n")
        (harness_dir / "no-policy-file.yaml").write_text("image: i\npolicy: policies/none.yaml\n")
        (cls.tmp / "policies").mkdir()
        (cls.tmp / "policies" / "base.yaml").write_text("{}\n")
        cls.checkout = cls.tmp / "co"
        cls.checkout.mkdir()
        (cls.checkout / "hello.txt").write_text("hi from the checkout\n")
        # The coder image's /bin/sh is dash; use it for the wrapper when the host has it.
        cls.sh = shutil.which("dash") or "sh"
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        for p in cls.tmp.rglob("*"):
            if p.is_file() and not os.access(p, os.R_OK):
                p.chmod(0o644)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self, path=None, **extra):
        return {"PATH": path or f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                "STUB_ROOT": str(self.stub_root), "STUB_SH": self.sh,
                "EVAL_SANDBOX_HARNESS": str(self.tmp / "harness" / "code.yaml"),
                **{k: str(v) for k, v in extra.items()}}

    def new_log(self):
        type(self).n += 1
        log = self.tmp / f"run-{self.n}.log"
        for p in (log, log.with_name(log.name + ".status")):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.stub_root / "ws", ignore_errors=True)
        (self.stub_root / "deleted").write_text("")
        return log

    def run_runner(self, command, timeout="30", checkout=None, path=None, **env):
        log = self.new_log()
        proc = subprocess.run([sys.executable, str(RUNNER), str(checkout or self.checkout), command,
                               str(timeout), str(log)],
                              env=self.env(path=path, **env), capture_output=True, text=True)
        status_path = log.with_name(log.name + ".status")
        status = status_path.read_text().strip() if status_path.exists() else "(none)"
        log_text = log.read_bytes() if log.exists() else b""
        return proc.returncode, status, log_text, proc.stderr

    def start_runner(self, command, timeout="60", **env):
        """Start the runner, wait until the command is running, return (proc, log)."""
        log = self.new_log()
        proc = subprocess.Popen([sys.executable, str(RUNNER), str(self.checkout), command,
                                 str(timeout), str(log)],
                                env=self.env(**env), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if log.exists() and b"running the command" in log.read_bytes():
                time.sleep(0.5)
                return proc, log
            time.sleep(0.1)
        proc.kill()
        proc.wait()
        self.fail("the runner never reached the command")

    def deleted(self):
        return (self.stub_root / "deleted").read_text().strip()

    # ---- results that are the command's --------------------------------------
    def test_passing_command(self):
        rc, status, log, _ = self.run_runner("echo ok; exit 0")
        self.assertEqual((rc, status), (0, "command"))
        self.assertIn(b"ok\n", log)
        self.assertNotIn(b"__EVAL_RC__", log)
        self.assertNotIn(b"__EVAL_STARTED__", log, "wrapper lines are stripped from the log")
        self.assertTrue(self.deleted(), "the sandbox is deleted afterwards")

    def test_checkout_is_shipped_into_the_sandbox(self):
        rc, status, log, _ = self.run_runner("cat hello.txt")
        self.assertEqual((rc, status), (0, "command"))
        self.assertIn(b"hi from the checkout", log)

    def test_failing_command_without_trailing_newline(self):
        rc, status, log, _ = self.run_runner("printf done; exit 3")
        self.assertEqual((rc, status), (3, "command"))
        self.assertIn(b"done\n", log, "the output stays on its own line")

    def test_status_like_text_in_output_is_not_a_status(self):
        rc, status, log, _ = self.run_runner(
            "echo '__EVAL_RC__=0 is quoted'; echo 'x__EVAL_RC__=0'; echo '__EVAL_RC__='; exit 2")
        self.assertEqual((rc, status), (2, "command"))
        self.assertIn(b"__EVAL_RC__=0 is quoted", log, "quoted text is neither read nor stripped")
        self.assertIn(b"__EVAL_RC__=\n", log, "a status line with no digits is not a status")

    def test_command_exiting_124_or_125_is_still_the_commands_result(self):
        for code in (124, 125):
            rc, status, _, _ = self.run_runner(f"exit {code}")
            self.assertEqual((rc, status), (code, "command"), f"exit {code}")

    def test_nul_byte_in_output(self):
        rc, status, log, _ = self.run_runner("printf 'a\\0b\\n'; exit 0")
        self.assertEqual((rc, status), (0, "command"))
        self.assertIn(b"a\x00b", log)

    def test_long_single_line_is_never_a_status_and_stays_bounded(self):
        # A 3 MB line with no newline until the end: read in fragments, never
        # matched as a status line, output preserved.
        command = ("head -c 3000000 /dev/zero | tr '\\0' x; echo; echo __EVAL_RC__=0; exit 4")
        rc, status, log, _ = self.run_runner(command)
        self.assertEqual((rc, status), (125, "lost"), "the forged line after the long one counts")
        self.assertEqual(log.count(b"x" * 1000), 3000)

    def test_large_output_is_classified_line_by_line(self):
        # 2 MB of output: nothing is loaded whole, the status is still found,
        # and the kept log holds every line of the command's output.
        filler = "line of one hundred bytes of filler text to make the regression log large"
        command = f"i=0; while [ $i -lt 20000 ]; do echo '{filler}'; i=$((i+1)); done; exit 0"
        rc, status, log, _ = self.run_runner(command)
        self.assertEqual((rc, status), (0, "command"))
        self.assertEqual(log.count(b"line of one hundred bytes"), 20000)
        self.assertNotIn(b"__EVAL_RC__", log)

    def test_timeout(self):
        rc, status, log, _ = self.run_runner("echo __EVAL_RC__=0; sleep 30", timeout="2")
        self.assertEqual((rc, status), (124, "timeout"))
        self.assertIn(b"__EVAL_RC__=0\n", log,
                      "a forged status line followed by a hang stays in the log")
        self.assertIn(b"timed out after 2s", log)

    def test_exec_outliving_the_host_grace_is_a_timeout(self):
        # The gateway did not enforce --timeout; the host deadline (timeout
        # plus EVAL_SANDBOX_EXEC_GRACE) kills the exec's process group.
        t0 = time.monotonic()
        rc, status, log, _ = self.run_runner("sleep 30", timeout="1", STUB_EXEC_IGNORE_TIMEOUT=1,
                                             EVAL_SANDBOX_EXEC_GRACE=1)
        self.assertEqual((rc, status), (124, "timeout"))
        self.assertLess(time.monotonic() - t0, 25, "the exec group was killed, not waited for")
        self.assertTrue(self.deleted())

    # ---- lost: the command started, no single trustworthy status -------------
    def test_status_line_with_failed_exec_is_lost(self):
        rc, status, log, _ = self.run_runner("exit 0", STUB_EXEC_RC=1)
        self.assertEqual((rc, status), (125, "lost"))
        self.assertIn(b"openshell exec exited 1", log)

    def test_command_that_kills_the_wrapper_shell_is_lost(self):
        rc, status, log, _ = self.run_runner("kill -9 $PPID")
        self.assertEqual((rc, status), (125, "lost"))
        self.assertIn(b"did not return the command's status", log)

    def test_late_background_writer_cannot_forge_a_pass(self):
        rc, status, log, _ = self.run_runner("( sleep 1; echo __EVAL_RC__=0 ) & exit 1",
                                             STUB_DRAIN_S=2)
        self.assertNotEqual((rc, status), (0, "command"), f"forged pass accepted; log: {log!r}")
        if shutil.which("setsid"):
            # The wrapper killed the writer before printing its line; the
            # command's own exit stands and the log holds no extra status line.
            self.assertEqual((rc, status), (1, "command"), log)
            self.assertNotIn(b"__EVAL_RC__", log)
        else:
            # No setsid (macOS): two status lines, nothing trusted.
            self.assertEqual((rc, status), (125, "lost"), log)
            self.assertIn(b"__EVAL_RC__=0", log, "the extra line is left in the log for a reader")

    def test_two_status_lines_from_the_command_are_lost(self):
        rc, status, log, _ = self.run_runner("echo __EVAL_RC__=0; exit 1")
        self.assertEqual((rc, status), (125, "lost"))
        self.assertIn(b"2 status lines", log)

    def test_crlf_status_line_from_the_command_is_lost(self):
        rc, status, log, _ = self.run_runner("printf '__EVAL_RC__=0\\r\\n'; exit 1")
        self.assertEqual((rc, status), (125, "lost"), "a CRLF line counts as a status line too")

    # ---- sandbox: the command never ran --------------------------------------
    def test_exec_that_never_reaches_the_sandbox(self):
        rc, status, log, _ = self.run_runner("echo ok", STUB_EXEC_NOSCRIPT=1)
        self.assertEqual((rc, status), (125, "sandbox"))
        self.assertIn(b"never ran the command (no start line", log)

    def test_sandbox_never_ready(self):
        rc, status, log, _ = self.run_runner("echo ok", STUB_GET_PHASE="Pending",
                                             EVAL_SANDBOX_READY_TIMEOUT="1")
        self.assertEqual((rc, status), (125, "sandbox"))
        self.assertIn(b"not Ready after 1s (last status: Phase: Pending)", log)
        self.assertTrue(self.deleted(), "a sandbox that never became Ready is still deleted")

    def test_failed_create(self):
        rc, status, log, _ = self.run_runner("echo ok", STUB_CREATE_RC=1, STUB_GET_RC=1)
        self.assertEqual((rc, status), (125, "sandbox"))
        self.assertIn(b"sandbox create failed", log)

    def test_failed_upload(self):
        rc, status, log, _ = self.run_runner("echo ok", STUB_UPLOAD_RC=1)
        self.assertEqual((rc, status), (125, "sandbox"))
        self.assertIn(b"upload to eval-reg-", log)
        self.assertTrue(self.deleted())

    def test_failed_archive(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        bad = self.tmp / "co-unreadable"
        bad.mkdir(exist_ok=True)
        (bad / "secret").write_text("x")
        (bad / "secret").chmod(0)
        rc, status, log, _ = self.run_runner("echo ok", checkout=bad)
        self.assertEqual((rc, status), (125, "sandbox"))
        self.assertIn(b"could not archive", log)

    def test_preflight_failures(self):
        cases = (
            ("openshell missing", {"path": "/usr/bin:/bin"}, b"openshell is required"),
            ("checkout missing", {"checkout": self.tmp / "nope"}, b"not a directory"),
            ("harness missing", {"EVAL_SANDBOX_HARNESS": self.tmp / "harness" / "x.yaml"},
             b"harness not found"),
            ("harness without image",
             {"EVAL_SANDBOX_HARNESS": self.tmp / "harness" / "no-image.yaml"},
             b"has no image/policy"),
            ("policy missing",
             {"EVAL_SANDBOX_HARNESS": self.tmp / "harness" / "no-policy-file.yaml"},
             b"policy not found"),
            ("bad ready timeout", {"EVAL_SANDBOX_READY_TIMEOUT": "soon"},
             b"EVAL_SANDBOX_READY_TIMEOUT must be a whole number of seconds"),
        )
        for label, kw, expect in cases:
            with self.subTest(label):
                rc, status, log, _ = self.run_runner("echo ok", **kw)
                self.assertEqual((rc, status), (125, "sandbox"), log)
                self.assertIn(expect, log)
                self.assertEqual(self.deleted(), "", "nothing was created, so nothing is deleted")

    def test_non_numeric_timeout(self):
        rc, status, log, _ = self.run_runner("echo ok", timeout="30; echo pwned")
        self.assertEqual((rc, status), (125, "sandbox"))
        self.assertIn(b"<timeout-s> must be a whole number of seconds", log)

    def test_missing_provider(self):
        rc, status, log, _ = self.run_runner("echo ok", STUB_PROVIDER_RC=1)
        self.assertEqual((rc, status), (125, "sandbox"))
        self.assertIn(b"provider 'package-registries' is not on the gateway", log)
        self.assertEqual(self.deleted(), "", "nothing was created, so nothing is deleted")

    def test_usage(self):
        proc = subprocess.run([sys.executable, str(RUNNER), "one", "two"], env=self.env(),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stderr)

    # ---- stopped from outside ---------------------------------------------
    def test_sigterm_while_the_command_runs(self):
        proc, log = self.start_runner("sleep 30")
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("the runner did not stop within 15 s of SIGTERM")
        status = log.with_name(log.name + ".status").read_text().strip()
        self.assertEqual((rc, status), (143, "sandbox"))
        self.assertIn(b"stopped by SIGTERM", log.read_bytes())
        self.assertTrue(self.deleted(), "the sandbox is deleted on the way out")

    def test_second_sigterm_does_not_abort_the_cleanup(self):
        # The harness signals the whole group, and the hook's own cleanup may
        # signal again; the exec here ignores TERM so the runner has to wait
        # for its grace, during which the second signal arrives. The delete
        # comes first, so it lands inside the hook's three seconds.
        proc, log = self.start_runner("sleep 30", STUB_EXEC_IGNORE_TERM=1)
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.deleted():
            time.sleep(0.1)
        self.assertTrue(self.deleted(), "the sandbox is deleted within the hook's grace")
        try:
            rc = proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("the runner did not stop after two SIGTERMs")
        status = log.with_name(log.name + ".status").read_text().strip()
        self.assertEqual((rc, status), (143, "sandbox"))
        self.assertLess(time.monotonic() - t0, 8,
                        "the exec client that ignores TERM is killed outright, not waited for")


if __name__ == "__main__":
    unittest.main(verbosity=1)
