#!/usr/bin/env python3
"""Run a repository's test command on a checkout inside a throwaway OpenShell sandbox.

Usage:
    run-in-sandbox.py <checkout-dir> <command> <timeout-s> <log-file>

The checkout is agent-written code built from untrusted inputs (or, for
check-case.sh, an upstream snapshot). On the eval host it would run with the
host's home directory in reach: gcloud application-default credentials, the
gh config, ~/.npmrc, ~/.ssh. So it runs where the coding agent itself ran:

  - image:    the coder's sandbox image from harness/code.yaml (same toolchains
              the agent used to run these tests, same pin)
  - policy:   policies/base.yaml from the same harness
  - network:  only the package-registries provider is attached. It carries no
              credential and opens the npm, PyPI and Go module registries to
              the package managers only; everything else, GitHub included, is
              unreachable. fullsend creates that provider on the gateway for
              every coder run, so it exists wherever the coder has run once.
  - identity: the sandbox user, HOME=/sandbox, no host environment at all
              (the openshell CLI does not forward its own environment into
              `sandbox exec`; checked against a live gateway, see below)

Environment: EVAL_SANDBOX_HARNESS (default harness/code.yaml), EVAL_SANDBOX_PROVIDER
(default package-registries), EVAL_SANDBOX_MEMORY (default 8Gi),
EVAL_SANDBOX_READY_TIMEOUT (seconds to wait for Ready, default 900),
EVAL_SANDBOX_EXEC_GRACE (seconds past <timeout-s> before the exec client is
killed from the host, default 60).

Exit status: the command's own exit code; 124 when the command hit the
timeout; 125 when no result could be obtained (the log file says why); 143
when stopped by SIGTERM. Because a command can itself exit 124 or 125, the
classification is also written to <log-file>.status as one word, and callers
read that file, not the exit code:

  command  the exit code is the command's
  timeout  the command ran past <timeout-s>
  sandbox  the sandbox never ran the command: a setup check, create, Ready
           wait, upload or the remote preamble failed. Says nothing about the
           code under test.
  lost     the command started but no single trustworthy status came back:
           the sandbox stopped answering (a dropped relay, an eviction, an
           OOM kill of the whole sandbox), the log holds more than one status
           line, a status line came back from an exec that did not exit
           cleanly, or the status value is not an exit code. Scored as the
           tests not passing (eval.yaml, regression_tests_pass).

How the two are told apart: the remote wrapper prints a start line
(__EVAL_STARTED__) immediately before the command and a status line
(__EVAL_RC__=N) immediately after it. "sandbox" means no start line; "lost"
means a start line and no usable status line. The command cannot forge the
start line, since it must appear before the command runs. It can print a
status line of its own, which is why the wrapper kills every process the
command left behind before printing the real one (the command runs in its own
session when the image has setsid, so the group can be killed; without setsid
the kill is skipped and the host's one-status-line rule is the only defence),
and why a log with more than one status line is "lost" rather than trusted,
with the extra lines left in the log for a reader. Only a whole line of the
exact shape counts, so test output that quotes the text is neither read as a
status nor stripped. The log is read in bounded chunks as bytes: a line longer
than a chunk can never be a status line, and neither the log's size nor a NUL
in the test output can derail the match or exhaust memory. The log itself is
not capped on the host; a suite that fills the disk ends as a capture failure
through the hook's provisional marker.

Three shapes are policy rather than proof, and are stated in eval.yaml and
eval/local/code-rhai/README.md: a status line together with a non-zero exec
exit is "lost" (a command could print one status line and kill the wrapper
shell to escape grading, so that shape cannot be trusted either way); a
timed-out exec with no start line is "timeout" (openshell may buffer output,
so the line's absence at the timeout is not evidence the command never ran);
and a failed exec with no start line is "sandbox", which assumes the relay
delivers the start line before a command's first action can take the whole
sandbox down.

The one defence that depends on the gateway: a command that prints a status
line and then kills the wrapper shell leaves exactly one status line, and is
caught only because `openshell sandbox exec` exits non-zero when the remote
shell dies by signal. Checked against a live gateway on 2026-10-18 (openshell
0.1.4, the pinned fullsend-code image, /bin/sh is dash, setsid present): the
exec exits 137 when the remote shell is killed, with or without a status line
already printed; a clean `exit 3` comes back as 3; a writer that outlives the
shell still lands in the log, so the straggler kill is needed; the client
environment is not forwarded into the sandbox; and output streams, the first
line visible within a second of being printed. The unit tests prove the same
shapes with a local `sh`.

Accepted gap: the checkout is archived on the host, uploaded and extracted in
the sandbox before the start line, so a PR head large enough to break any of
those steps is recorded as "sandbox" (a capture failure), although the content
is the agent's.

The log file holds the command's combined output with the two wrapper lines
removed when exactly one status line was found.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, NoReturn

START_LINE = b"__EVAL_STARTED__"
STATUS_RE = re.compile(rb"^__EVAL_RC__=(\d{1,3})$")
EXIT_TIMEOUT, EXIT_SANDBOX, EXIT_TERM = 124, 125, 143
DELETE_TIMEOUT_S = 2.5  # inside the hook's 3 s grace and the harness's 5 s
MAX_LINE = 1 << 20  # a line longer than this is read in fragments and is never a status line
UTF8 = "utf-8"
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class Terminated(Exception):
    """SIGTERM, SIGINT or SIGHUP arrived; the harness kills the hook's process group."""


def _on_signal(signum, _frame):
    raise Terminated(signal.Signals(signum).name)


def have_pyyaml() -> bool:
    return importlib.util.find_spec("yaml") is not None


def load_yaml(path: Path) -> dict:
    """PyYAML when present (the harness installs it); yq otherwise."""
    if have_pyyaml():
        import yaml
        return yaml.safe_load(path.read_text(encoding=UTF8)) or {}
    out = subprocess.run(["yq", "-o=json", ".", str(path)], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out) or {}


def strip_eol(line: bytes) -> bytes:
    return line.rstrip(b"\n").rstrip(b"\r")


def log_chunks(fh) -> Iterator[tuple[int, bytes, bool]]:
    """Yield (line index, chunk, whole) over a log without holding a line in memory.

    A chunk is at most MAX_LINE bytes. `whole` is True when the chunk is an
    entire line (it starts at a line start and ends at a newline or at the end
    of the file within the limit); a line longer than the limit comes out as
    fragments, none of them whole, and so can never match a wrapper line.
    """
    index, at_start = 0, True
    while True:
        chunk = fh.readline(MAX_LINE)
        if not chunk:
            return
        ends = chunk.endswith(b"\n")
        yield index, chunk, at_start and (ends or len(chunk) < MAX_LINE)
        if ends:
            index += 1
        at_start = ends


class Runner:
    def __init__(self, checkout: Path, command: str, timeout_s: str, log: Path,
                 env: dict[str, str]):
        self.env = env
        self.checkout = checkout
        self.command = command
        self.log = log
        self.status_file = log.with_name(log.name + ".status")
        self.agents_root = Path(__file__).resolve().parent.parent.parent
        self.harness = Path(env.get("EVAL_SANDBOX_HARNESS")
                            or self.agents_root / "harness" / "code.yaml")
        self.provider = env.get("EVAL_SANDBOX_PROVIDER", "package-registries")
        self.memory = env.get("EVAL_SANDBOX_MEMORY", "8Gi")
        # Validated in preflight(), so a bad value is reported through the
        # status file like every other setup failure.
        self.timeout_raw = timeout_s
        self.ready_raw = env.get("EVAL_SANDBOX_READY_TIMEOUT", "900")
        self.grace_raw = env.get("EVAL_SANDBOX_EXEC_GRACE", "60")
        self.timeout_s = 0
        self.ready_timeout = 0
        self.exec_grace = 0
        # openshell caps names at 19 characters.
        self.name = "eval-reg-" + secrets.token_hex(4)
        self.tarball: Path | None = None
        self.created = False
        self.child: subprocess.Popen | None = None
        self.log.write_bytes(b"")
        self.write_status("sandbox")  # until the command reports its own status

    # ---- helpers -----------------------------------------------------------
    def whole_seconds(self, label: str, value: object) -> int:
        if value is None or not re.fullmatch(r"[0-9]+", str(value)):
            self.fail_sandbox(f"run-in-sandbox: {label} must be a whole number of seconds, "
                              f"got '{value}'")
        return int(str(value))

    def note(self, msg: str) -> None:
        with self.log.open("ab") as fh:
            fh.write(msg.encode() + b"\n")
        print(msg, file=sys.stderr)

    def write_status(self, word: str) -> None:
        self.status_file.write_text(word + "\n", encoding=UTF8)

    def fail_sandbox(self, msg: str) -> NoReturn:
        self.note(msg)
        self.write_status("sandbox")
        raise SystemExit(EXIT_SANDBOX)

    def fail_lost(self, msg: str) -> NoReturn:
        self.note(msg)
        self.write_status("lost")
        raise SystemExit(EXIT_SANDBOX)

    def openshell(self, *args: str, timeout: float | None = None,
                  capture: bool = False) -> subprocess.CompletedProcess:
        # Every openshell call takes stdin from /dev/null: the CLI streams
        # stdin to the remote command, and an inherited pipe that never closes
        # makes `exec` hang.
        if capture:
            return subprocess.run(["openshell", *args], stdin=subprocess.DEVNULL, env=self.env,
                                  capture_output=True, text=True, timeout=timeout)
        with self.log.open("ab") as fh:
            return subprocess.run(["openshell", *args], stdin=subprocess.DEVNULL, env=self.env,
                                  stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)

    # ---- phases ------------------------------------------------------------
    def preflight(self) -> tuple[str, Path]:
        # The timeouts reach arithmetic and a CLI flag; accept whole seconds only.
        self.timeout_s = self.whole_seconds("<timeout-s>", self.timeout_raw)
        self.ready_timeout = self.whole_seconds("EVAL_SANDBOX_READY_TIMEOUT", self.ready_raw)
        self.exec_grace = self.whole_seconds("EVAL_SANDBOX_EXEC_GRACE", self.grace_raw)
        for tool in ("openshell", "tar"):
            if shutil.which(tool, path=self.env.get("PATH")) is None:
                self.fail_sandbox(f"run-in-sandbox: {tool} is required but not found in PATH")
        if not self.checkout.is_dir():
            self.fail_sandbox(f"run-in-sandbox: not a directory: {self.checkout}")
        if not self.harness.is_file():
            self.fail_sandbox(f"run-in-sandbox: harness not found: {self.harness}")
        cfg = load_yaml(self.harness)
        image, policy_rel = str(cfg.get("image") or ""), str(cfg.get("policy") or "")
        if not image or not policy_rel:
            self.fail_sandbox(f"run-in-sandbox: {self.harness} has no image/policy")
        policy = self.harness.parent.parent / policy_rel
        if not policy.is_file():
            policy = self.agents_root / policy_rel
        if not policy.is_file():
            self.fail_sandbox(f"run-in-sandbox: policy not found: {policy_rel}")
        if self.openshell("provider", "get", self.provider, capture=True).returncode != 0:
            self.fail_sandbox(f"run-in-sandbox: OpenShell provider '{self.provider}' is not on "
                              "the gateway. fullsend creates it on the first coder run; run one, "
                              "or create it from fullsend's fullsend-package-registries profile.")
        return image, policy

    def create(self, image: str, policy: Path) -> None:
        self.note(f"run-in-sandbox: creating {self.name} from {image}")
        self.created = True  # from here on, cleanup deletes it whatever happens
        rc = self.openshell("sandbox", "create", "--name", self.name, "--keep",
                            "--no-auto-providers", "--no-tty", "--from", image,
                            "--policy", str(policy), "--provider", self.provider,
                            "--memory", self.memory, "--detach",
                            "--", "sleep", "infinity").returncode
        if rc != 0 and self.openshell("sandbox", "get", self.name, capture=True).returncode != 0:
            self.fail_sandbox("run-in-sandbox: sandbox create failed")
        deadline = time.monotonic() + self.ready_timeout
        state = ""
        while True:
            got = self.openshell("sandbox", "get", self.name, capture=True)
            state = got.stdout if got.returncode == 0 else state
            if "Phase: Ready" in state:
                return
            if time.monotonic() > deadline:
                phase = next((ln.strip() for ln in state.splitlines()
                              if ln.strip().startswith("Phase:")), "unknown")
                self.fail_sandbox(f"run-in-sandbox: sandbox {self.name} not Ready after "
                                  f"{self.ready_timeout}s (last status: {phase})")
            time.sleep(3)

    def upload(self) -> str:
        # Ship the checkout as one archive: openshell's own upload filters by
        # .gitignore and skips symlink targets; a tarball keeps the tree exact.
        fd, name = tempfile.mkstemp(prefix="eval-reg-", dir=self.env.get("TMPDIR", "/tmp"))
        os.close(fd)
        self.tarball = Path(name)
        with self.log.open("ab") as fh:
            rc = subprocess.run(["tar", "--no-xattrs", "-czf", str(self.tarball),
                                 "-C", str(self.checkout), "."],
                                env={**self.env, "COPYFILE_DISABLE": "1"},
                                stdout=fh, stderr=subprocess.STDOUT).returncode
        if rc != 0:
            self.fail_sandbox(f"run-in-sandbox: could not archive {self.checkout}")
        up = self.openshell("sandbox", "upload", self.name, str(self.tarball), "/tmp/")
        if up.returncode != 0:
            self.fail_sandbox(f"run-in-sandbox: upload to {self.name} failed")
        return "/tmp/" + self.tarball.name

    def remote_script(self, remote_tar: str) -> str:
        # The sandbox shell does not inherit the image's PATH; set what the
        # image itself declares, plus the Go env the coder harness sets. The
        # egress proxy drops a few connections when a Go build fetches ~200
        # modules at once; a pipe-separated GOPROXY list retries the same
        # proxy, since Go falls through on any error at a '|'. npm gets
        # retries and fewer sockets for the same reason. Browser-binary
        # post-install downloads go to hosts the policy does not allow and
        # nothing in the sandbox can run a browser, so they are skipped.
        #
        # The command runs in its own session (setsid) when the image has it,
        # so that everything it leaves behind can be killed before the status
        # line is printed. The remote shell is dash in the coder image, whose
        # kill builtin takes no `--` after the signal; `-<pgid>` alone parses
        # in dash, bash and busybox ash. Without setsid the kill is skipped and
        # the host's one-status-line rule is the only defence against a late
        # writer.
        cmd = shlex.quote(self.command)
        return f"""set -e
export PATH=/usr/local/go/bin:/sandbox/.venv/bin:/usr/local/bin:/usr/bin:/bin:/sandbox/go/bin
export HOME=/sandbox GOPATH=/sandbox/go GOMODCACHE=/sandbox/go/pkg/mod LANG=C.UTF-8 TERM=dumb CI=1
export GOPROXY='https://proxy.golang.org|https://proxy.golang.org|https://proxy.golang.org'
export npm_config_fetch_retries=5 npm_config_fetch_retry_mintimeout=20000 \
npm_config_fetch_retry_maxtimeout=120000 npm_config_maxsockets=6
export CYPRESS_INSTALL_BINARY=0 PUPPETEER_SKIP_DOWNLOAD=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
mkdir -p /sandbox/workspace/regression
tar -xzf {shlex.quote(remote_tar)} -C /sandbox/workspace/regression
cd /sandbox/workspace/regression
printf '\\n{START_LINE.decode()}\\n'
cmd_rc=0
if command -v setsid >/dev/null 2>&1; then
  setsid bash -o pipefail -c {cmd} </dev/null & pid=$!
  wait "$pid" || cmd_rc=$?
  kill -9 "-$pid" 2>/dev/null || true
else
  bash -o pipefail -c {cmd} </dev/null || cmd_rc=$?
fi
printf '\\n__EVAL_RC__=%s\\n' "$cmd_rc"
"""

    def execute(self, remote_tar: str) -> tuple[int, int, bool]:
        """Run the wrapper; return (exec exit code, elapsed seconds, timed out)."""
        self.note(f"run-in-sandbox: running the command (timeout {self.timeout_s}s)")
        t0 = time.monotonic()
        with self.log.open("ab") as fh:
            self.child = subprocess.Popen(
                ["openshell", "sandbox", "exec", "--name", self.name, "--no-tty",
                 "--timeout", str(self.timeout_s), "--", "sh", "-c",
                 self.remote_script(remote_tar)],
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, env=self.env,
                start_new_session=True)
            timed_out = False
            try:
                rc = self.child.wait(timeout=self.timeout_s + self.exec_grace)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill_child()
                rc = EXIT_TIMEOUT
        elapsed = int(time.monotonic() - t0)
        self.note(f"run-in-sandbox: openshell exec exited {rc} after {elapsed}s")
        return rc, elapsed, timed_out

    def kill_child(self, hard: bool = False) -> None:
        # SIGTERM the exec's process group, then SIGKILL: a relay client that
        # ignores TERM must not keep the runner alive past the test timeout.
        # `hard` goes straight to SIGKILL, for the cleanup that must fit in the
        # hook's three-second grace.
        if self.child is None or self.child.poll() is not None:
            return
        steps = ((signal.SIGKILL, 2),) if hard else ((signal.SIGTERM, 10), (signal.SIGKILL, 5))
        for sig, grace in steps:
            try:
                os.killpg(self.child.pid, sig)
                self.child.wait(timeout=grace)
                return
            except (ProcessLookupError, PermissionError):
                return
            except subprocess.TimeoutExpired:
                continue

    def scan_log(self) -> tuple[int | None, list[tuple[int, int]]]:
        """One bounded pass over the log: (index of the start line, [(index, rc)])."""
        start, statuses = None, []
        with self.log.open("rb") as fh:
            for index, chunk, whole in log_chunks(fh):
                if not whole:
                    continue
                line = strip_eol(chunk)
                if line == START_LINE and start is None:
                    start = index
                m = STATUS_RE.match(line)
                if m:
                    statuses.append((index, int(m.group(1))))
        return start, statuses

    def strip_lines(self, indices: set[int]) -> None:
        """Rewrite the log without the given line indices (the two wrapper lines)."""
        tmp = self.log.with_name(self.log.name + ".tmp")
        with self.log.open("rb") as src, tmp.open("wb") as dst:
            for index, chunk, _ in log_chunks(src):
                if index not in indices:
                    dst.write(chunk)
        os.replace(tmp, self.log)

    def classify(self, exec_rc: int, elapsed: int, timed_out: bool) -> int:
        # A timeout outranks any status line: the wrapper prints its line and
        # exits at once, so a status line followed by a run that hit the limit
        # was printed by the command itself.
        if timed_out or exec_rc == EXIT_TIMEOUT or elapsed >= self.timeout_s:
            self.note(f"run-in-sandbox: command timed out after {self.timeout_s}s")
            self.write_status("timeout")
            return EXIT_TIMEOUT
        start, statuses = self.scan_log()
        if start is None:
            self.fail_sandbox("run-in-sandbox: the sandbox never ran the command (no start line; "
                              f"openshell exec exit {exec_rc} after {elapsed}s)")
        if len(statuses) > 1:
            self.fail_lost(f"run-in-sandbox: {len(statuses)} status lines in the log where the "
                           "wrapper prints one; the command printed the others (left in the log), "
                           "so no status is trusted")
        if len(statuses) == 1 and exec_rc != 0:
            # The wrapper exits 0 once it has printed its status, so a status
            # line on an exec that failed means the wrapper never finished.
            self.fail_lost(f"run-in-sandbox: a status line is present but openshell exec exited "
                           f"{exec_rc} after {elapsed}s; the sandbox did not finish cleanly")
        if not statuses:
            self.fail_lost("run-in-sandbox: the sandbox did not return the command's status "
                           f"(openshell exec exit {exec_rc} after {elapsed}s)")
        index, rc = statuses[0]
        if rc > 255:
            self.fail_lost(f"run-in-sandbox: status line carries {rc}, not an exit code")
        self.strip_lines({start, index})
        self.write_status("command")
        return rc

    def cleanup(self) -> None:
        # The delete comes first and is bounded: at the hook timeout the hook
        # gives this process three seconds and the harness five, and a
        # gateway that is not answering is the likely reason for being here
        # at all. The exec client is then killed outright, since it runs in
        # its own session where no caller's group signal reaches it.
        if self.created:
            try:
                self.openshell("sandbox", "delete", self.name, capture=True,
                               timeout=DELETE_TIMEOUT_S)
            except (subprocess.TimeoutExpired, OSError):
                pass
        self.kill_child(hard=True)
        if self.tarball is not None:
            try:
                self.tarball.unlink()
            except OSError:
                pass

    def run(self) -> int:
        image, policy = self.preflight()
        self.create(image, policy)
        remote_tar = self.upload()
        return self.classify(*self.execute(remote_tar))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4:
        print("usage: run-in-sandbox.py <checkout-dir> <command> <timeout-s> <log-file>",
              file=sys.stderr)
        return 2
    for sig in SIGNALS:
        signal.signal(sig, _on_signal)
    runner = Runner(Path(argv[0]), argv[1], argv[2], Path(argv[3]), dict(os.environ))
    try:
        return runner.run()
    except Terminated as t:
        # Stopped from outside (the harness hook timeout): no result, and the
        # caller must not read a stale status. A repeat signal (the hook's own
        # cleanup, the harness's second pass) must not interrupt the cleanup
        # that deletes the sandbox, so further signals are ignored from here.
        for sig in SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        runner.note(f"run-in-sandbox: stopped by {t} before the command reported a status")
        runner.write_status("sandbox")
        return EXIT_TERM
    finally:
        for sig in SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        runner.cleanup()


if __name__ == "__main__":
    sys.exit(main())
