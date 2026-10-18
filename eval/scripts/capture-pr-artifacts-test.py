#!/usr/bin/env python3
"""Tests for capture-pr-artifacts.py.

The hook's one rule: every way it can end leaves output/agent-outcome.json,
and every way the eval (not the agent) can fail also leaves
output/infrastructure-failure.json, the marker run-functional.sh counts as a
case failure.

Covered: a missing tool, a missing or unparsable fixture record, a PR list
GitHub would not return, a malformed head ref, base ref or repo name, a bad
PR_DIFF_MAX_BYTES, a failing gh call, a failing final-message extractor, a run
whose status was not recorded; declines and ordinary failures without a PR;
failures after the outcome is known (a PR head that will not clone, a diff
GitHub would not return with and without the local fallback, a sandbox that
will not start, a runner that dies before writing its status or writes a word
the hook does not know, a PR head with a file that will not copy, an
unexpected error after the outcome, SIGTERM and SIGKILL during the tests); the
exit-time check catching a gap no named handler saw; the PR path with every
sandbox status, including "lost", which is a scored result; symlinks in the
PR head, including a loop; diff truncation; the tail of the test log; the
compact JSON shape the judges' conditions match; and the judges' own check
bodies from eval.yaml, run against what the hook wrote.

Stub gh, git, openshell, run-in-sandbox.py and extract-final-message.py on
PATH stand in for the network and the sandbox.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "capture-pr-artifacts.py"
EVAL_YAML = HERE.parent / "local" / "code-rhai" / "eval.yaml"

PR_VIEW = json.dumps({
    "number": 7, "url": "https://x/pull/7", "title": "T",
    "body": "The agent wrote this.\n\n### Post-script verification\n- [x] branch ok",
    "headRefName": "agent/7-x", "baseRefName": "main", "headRefOid": "abc",
    "additions": 1, "deletions": 0, "files": [{"path": "a.txt"}],
})

STUB_GH = r'''#!/usr/bin/env bash
# STUB_GH_RC non-zero makes every call fail (a GitHub API error);
# STUB_GH_DIFF_RC non-zero makes only `pr diff` fail (a 406 on a huge PR).
[[ "${STUB_GH_RC:-0}" != 0 ]] && { echo "stub gh: failing on purpose" >&2; exit "$STUB_GH_RC"; }
case "$1 $2" in
  "pr view") printf '%s\n' "$STUB_PR_VIEW" ;;
  "pr diff")
    [[ "${STUB_GH_DIFF_RC:-0}" != 0 ]] \
      && { echo "stub gh: pr diff failing" >&2; exit "$STUB_GH_DIFF_RC"; }
    printf 'diff --git a/a.txt b/a.txt\n+hello from github\n' ;;
  *) exit 0 ;;
esac
'''

STUB_GIT = r'''#!/usr/bin/env bash
# clone: make the checkout (STUB_GIT_RC non-zero fails it; STUB_GIT_UNREADABLE_FILE
# adds a file nothing can read; STUB_GIT_UNREADABLE_DIR a directory nothing can
# list). fetch/diff: the local-diff fallback (STUB_GIT_DIFF_RC fails the diff; a
# fetch without the credential helper fails, as it would against the private
# fixture repo). STUB_GIT_REMOVE_PRMD deletes judge/pr.md after a successful
# clone, so that an artifact goes missing without any named failure path noticing.
args=("$@")
if [[ " ${args[*]} " == *" clone "* ]]; then
  [[ "${STUB_GIT_RC:-0}" != 0 ]] && { echo "stub git: clone failing" >&2; exit "$STUB_GIT_RC"; }
  for a in "$@"; do last="$a"; done
  mkdir -p "$last/sub" && echo hello > "$last/a.txt" && echo nested > "$last/sub/b.txt"
  ln -s a.txt "$last/ok-link"; ln -s /etc/passwd "$last/evil-link"
  ln -s ../../outside "$last/escape-link"; ln -s loop "$last/loop"
  if [[ "${STUB_GIT_UNREADABLE_FILE:-0}" == 1 ]]; then
    echo x > "$last/locked.txt"; chmod 000 "$last/locked.txt"
  fi
  [[ "${STUB_GIT_UNREADABLE_DIR:-0}" == 1 ]] && { mkdir "$last/vault"; chmod 000 "$last/vault"; }
  [[ "${STUB_GIT_REMOVE_PRMD:-0}" == 1 ]] && rm -f "$CASE_WORKSPACE/judge/pr.md"
  exit 0
fi
if [[ " ${args[*]} " == *" fetch "* ]]; then
  [[ " ${args[*]} " == *"credential.helper="* ]] \
    || { echo "stub git: fetch without the credential helper" >&2; exit 128; }
  [[ "${GIT_TERMINAL_PROMPT:-}" == 0 ]] \
    || { echo "stub git: fetch would prompt for a password" >&2; exit 128; }
  exit "${STUB_GIT_FETCH_RC:-0}"
fi
if [[ " ${args[*]} " == *" diff "* ]]; then
  [[ "${STUB_GIT_DIFF_RC:-0}" != 0 ]] \
    && { echo "stub git: diff failing" >&2; exit "$STUB_GIT_DIFF_RC"; }
  printf 'diff --git a/a.txt b/a.txt\n+hello from the checkout\n'; exit 0
fi
exit 0
'''

STUB_RUNNER = r'''#!/usr/bin/env bash
# <checkout> <command> <timeout> <log>: writes the status file the way the
# real runner does, from STUB_SANDBOX_STATUS (command|timeout|sandbox|lost)
# and STUB_SANDBOX_RC; STUB_SANDBOX_LOG_LINES=N writes N numbered lines to the
# log. STUB_SANDBOX_NO_STATUS=1: die before writing the status file, as a
# runner that never started would. STUB_SANDBOX_TERM=1: send the hook SIGTERM
# as the harness does at the hook timeout, then take STUB_SANDBOX_LINGER
# seconds to "clean up" (holding no pipe of the hook's, so the test measures
# the hook's own exit). STUB_SANDBOX_KILL9=1: SIGKILL the hook, as the harness
# does after its grace.
echo "stub ran: $2" > "$4"
if [[ -n "${STUB_SANDBOX_LOG_LINES:-}" ]]; then
  for ((i = 1; i <= STUB_SANDBOX_LOG_LINES; i++)); do echo "line$i"; done >> "$4"
fi
[[ "${STUB_SANDBOX_NO_STATUS:-0}" == 1 ]] && exit 125
[[ "${STUB_SANDBOX_KILL9:-0}" == 1 ]] && { kill -9 "$PPID"; exit 137; }
if [[ "${STUB_SANDBOX_TERM:-0}" == 1 ]]; then
  kill -TERM "$PPID"; exec >/dev/null 2>&1; sleep "${STUB_SANDBOX_LINGER:-0}"; exit 143
fi
echo "${STUB_SANDBOX_STATUS:-command}" > "$4.status"
exit "${STUB_SANDBOX_RC:-0}"
'''

STUB_EXTRACT = r'''#!/usr/bin/env bash
# STUB_EXTRACT_RC non-zero: the extractor crashes (a traceback, an unreadable transcript).
[[ "${STUB_EXTRACT_RC:-0}" != 0 ]] \
  && { echo "stub extractor: traceback" >&2; exit "$STUB_EXTRACT_RC"; }
printf '%s' "${STUB_FINAL_MSG:-}"
'''

PR_STATE = {"pull_requests": [{"number": 7, "state": "OPEN", "head": "agent/7-x"}]}
NO_PR_STATE = {"pull_requests": []}
CLEAN_RUN = {"exit_code": 0, "timed_out": False}


def have_pyyaml():
    return importlib.util.find_spec("yaml") is not None


def load_eval_yaml():
    if have_pyyaml():
        import yaml
        return yaml.safe_load(EVAL_YAML.read_text())
    out = subprocess.run(["yq", "-o=json", ".", str(EVAL_YAML)], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out)


class TestCaptureHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()
        stubs = (("gh", STUB_GH), ("git", STUB_GIT), ("run-in-sandbox.py", STUB_RUNNER),
                 ("extract-final-message.py", STUB_EXTRACT),
                 ("openshell", "#!/usr/bin/env bash\nexit 0\n"))
        for name, body in stubs:
            p = cls.bin / name
            p.write_text(body)
            p.chmod(0o755)
        # The hook reads YAML through PyYAML or yq; the hook PATH below carries
        # only the stubs and the system directories, so the yq binary alone is
        # linked in when the interpreter has no PyYAML (the CI script-test job,
        # for one). Only the binary: its directory may hold a real openshell.
        if not have_pyyaml():
            yq = shutil.which("yq")
            if yq is None:
                raise unittest.SkipTest("neither PyYAML nor yq is available")
            os.symlink(yq, cls.bin / "yq")
        # A PATH without one tool is a real absence, not a stub that exits non-zero.
        cls.bin_no_openshell = cls.tmp / "bin-no-openshell"
        cls.bin_no_openshell.mkdir()
        for p in cls.bin.iterdir():
            if p.name != "openshell":
                os.symlink(p, cls.bin_no_openshell / p.name)
        cls.judges = {j["name"]: j for j in load_eval_yaml()["judges"]}
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        for p in cls.tmp.rglob("*"):
            if not p.is_symlink() and not os.access(p, os.R_OK):
                p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- fixtures ----------------------------------------------------------
    def new_case(self, state, run_status=None, metrics=False, command="true"):
        type(self).n += 1
        ws = self.tmp / f"case-{self.n}"
        (ws / "output").mkdir(parents=True)
        (ws / "src" / "repo").mkdir(parents=True)
        (ws / "src" / "repo" / "README.md").write_text("snapshot\n")
        (ws / "src" / "input.yaml").write_text("fixture:\n  title: t\n  body: b\n")
        (ws / "src" / "annotations.yaml").write_text(
            f'judge_notes: ""\nregression_tests:\n  command: "{command}"\ntest_timeout_s: 60\n')
        if state is not None:
            text = state if isinstance(state, str) else json.dumps(state)
            (ws / "output" / "fixture-state.json").write_text(text)
        if run_status is not None:
            (ws / "output" / "run-status.json").write_text(json.dumps(run_status))
        if metrics:
            (ws / "output" / "fs-cod-1").mkdir()
            (ws / "output" / "fs-cod-1" / "metrics.json").write_text("{}")
        return ws

    def run_hook(self, ws, path=None, timeout=60, **env):
        full_env = {"PATH": f"{path or self.bin}{os.pathsep}/usr/bin:/bin",
                    "HOME": str(self.tmp), "CASE_WORKSPACE": str(ws),
                    "CASE_SOURCE_DIR": str(ws / "src"), "EPHEMERAL_REPO": "o/r",
                    "STUB_PR_VIEW": PR_VIEW, **{k: str(v) for k, v in env.items()}}
        proc = subprocess.run([sys.executable, str(HOOK)], env=full_env, capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout + proc.stderr

    @staticmethod
    def js(path):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def raw(path):
        return Path(path).read_text()

    def outcome(self, ws):
        o = self.js(ws / "output" / "agent-outcome.json")
        return None if o is None else f"{o.get('outcome')}/{str(o.get('infrastructure')).lower()}"

    def marker(self, ws):
        m = self.js(ws / "output" / "infrastructure-failure.json")
        return None if m is None else m.get("reason")

    def incomplete(self, ws):
        return (self.js(ws / "output" / "agent-outcome.json") or {}).get("artifacts_incomplete")

    def regression(self, ws):
        return self.js(ws / "output" / "regression.json") or {}

    def read_tests_md(self, ws):
        return self.raw(ws / "judge" / "tests.md")

    # ---- the judges, as eval.yaml defines them, against what the hook wrote ----
    def outputs_of(self, ws):
        """The `outputs` the harness hands a check, built the way score.py builds it.

        Files are keyed by case-relative path and read with the default
        encoding; a file that does not decode becomes a binary marker dict.
        """
        files = {}
        for top in ("output", "judge"):
            for p in (ws / top).rglob("*"):
                if p.is_file() and not p.is_symlink():
                    try:
                        files[str(p.relative_to(ws))] = p.read_text()
                    except UnicodeDecodeError:
                        files[str(p.relative_to(ws))] = {"_binary": True, "size": p.stat().st_size}
        return {"files": files, "annotations": {}}

    def check(self, name, ws):
        """Run judge `name`'s check body as score.py does: _check(outputs, arguments)."""
        body = self.judges[name]["check"]
        ns = {}
        exec("def _check(outputs, arguments):\n"
             + "".join(f"    {ln}\n" for ln in body.splitlines()), ns)
        return ns["_check"](self.outputs_of(ws), {})

    def condition(self, name, ws):
        """Evaluate judge `name`'s `if:` as score.py does: no builtins, annotations in scope."""
        return bool(eval(self.judges[name]["if"], {"__builtins__": {}},
                         {"annotations": {}, "outputs": self.outputs_of(ws)}))

    def test_judges_on_a_passing_pr(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check("pr_created", ws)[0], True)
        self.assertEqual(self.check("outcome", ws)[0], True)
        self.assertEqual(self.check("regression_tests_pass", ws)[0], True)
        self.assertTrue(self.condition("pr_quality", ws), "the quality judge runs on a PR")
        self.assertFalse(self.condition("decline_quality", ws))

    def test_judges_on_a_failing_pr_and_a_lost_sandbox(self):
        for status, rc in (("command", 3), ("lost", 125), ("timeout", 124)):
            with self.subTest(status=status):
                ws = self.new_case(PR_STATE)
                rc_hook, out = self.run_hook(ws, STUB_SANDBOX_STATUS=status, STUB_SANDBOX_RC=rc)
                self.assertEqual(rc_hook, 0, out)
                passed, why = self.check("regression_tests_pass", ws)
                self.assertFalse(passed, why)
                self.assertTrue(self.condition("pr_quality", ws), "a scored result is still judged")

    def test_judges_on_a_decline(self):
        ws = self.new_case(NO_PR_STATE, run_status=CLEAN_RUN, metrics=True)
        rc, out = self.run_hook(ws, STUB_FINAL_MSG="I stopped because the endpoint exists.")
        self.assertEqual(rc, 0, out)
        passed, why = self.check("outcome", ws)
        self.assertTrue(passed)
        self.assertIn("declined:", why)
        self.assertEqual(self.check("regression_tests_pass", ws)[0], True, "not applicable passes")
        self.assertTrue(self.condition("decline_quality", ws))
        self.assertFalse(self.condition("pr_quality", ws))

    def test_judges_on_a_capture_failure(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_SANDBOX_STATUS="sandbox", STUB_SANDBOX_RC=125)
        self.assertNotEqual(rc, 0)
        for name in ("pr_created", "outcome", "regression_tests_pass", "review_agent_ran"):
            with self.subTest(judge=name):
                with self.assertRaises(RuntimeError, msg="a capture failure is a judge error"):
                    self.check(name, ws)
        self.assertFalse(self.condition("pr_quality", ws), "the quality judge skips")
        self.assertFalse(self.condition("decline_quality", ws))

    def test_judges_on_a_pr_list_github_would_not_return(self):
        state = {"pull_requests": [], "pull_requests_error": "gh pr list failed after retries"}
        ws = self.new_case(state, run_status=CLEAN_RUN)
        rc, out = self.run_hook(ws)
        self.assertNotEqual(rc, 0, out)
        with self.assertRaises(RuntimeError):
            self.check("pr_created", ws)
        with self.assertRaises(RuntimeError):
            self.check("outcome", ws)

    # ---- infrastructure failures before the outcome: marker + failed/true ---
    def test_missing_tool(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, path=self.bin_no_openshell)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("not found in PATH: openshell", self.marker(ws))
        self.assertNotIn("yq", self.marker(ws), "only the absent tool is named")

    def test_missing_fixture_record(self):
        ws = self.new_case(None)
        rc, out = self.run_hook(ws)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("fixture-state.json missing", self.marker(ws))

    def test_unparsable_fixture_record(self):
        ws = self.new_case("{not json")
        rc, out = self.run_hook(ws)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("not valid JSON", self.marker(ws))

    def test_pr_list_github_would_not_return(self):
        state = {"pull_requests": [],
                 "pull_requests_error": "gh pr list failed for o/r after retries"}
        ws = self.new_case(state, run_status=CLEAN_RUN, metrics=True)
        rc, out = self.run_hook(ws, STUB_FINAL_MSG="I stopped.")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("PR list not captured", self.marker(ws))
        self.assertFalse((ws / "judge" / "repo-snapshot").exists(), "never a decline")

    def test_malformed_head_ref(self):
        state = {"pull_requests": [{"number": 7, "state": "OPEN", "head": "agent/7 x;"}]}
        ws = self.new_case(state)
        rc, out = self.run_hook(ws)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("unexpected head ref", self.marker(ws))

    def test_option_looking_base_ref(self):
        ws = self.new_case(PR_STATE)
        view = json.loads(PR_VIEW)
        view["baseRefName"] = "--upload-pack=evil"
        rc, out = self.run_hook(ws, STUB_PR_VIEW=json.dumps(view))
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("unexpected base ref", self.marker(ws))

    def test_malformed_repo_name(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, EPHEMERAL_REPO="o/r;evil")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("unexpected EPHEMERAL_REPO", self.marker(ws))

    def test_bad_diff_size_limit(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, PR_DIFF_MAX_BYTES="lots")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false",
                         "found when the diff is capped, after the outcome")
        self.assertTrue(self.incomplete(ws))
        self.assertIn("PR_DIFF_MAX_BYTES must be a whole number", self.marker(ws))

    def test_failing_gh_pr_view(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_GH_RC=1)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("gh pr view 7 exited 1", self.marker(ws))

    def test_failing_final_message_extractor(self):
        ws = self.new_case(NO_PR_STATE, run_status=CLEAN_RUN, metrics=True)
        rc, out = self.run_hook(ws, STUB_EXTRACT_RC=1, STUB_FINAL_MSG="never read")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/true")
        self.assertIn("final-message extractor exited 1", self.marker(ws))

    def test_run_status_not_recorded(self):
        ws = self.new_case(NO_PR_STATE, metrics=True)
        rc, out = self.run_hook(ws, STUB_FINAL_MSG="I stopped because the endpoint exists.")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.outcome(ws), "failed/true",
                         "run-fullsend.sh dying is the eval's failure, not the agent's")
        self.assertIn("run status not recorded", self.marker(ws))

    def test_unreadable_snapshot_directory_on_a_decline(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        ws = self.new_case(NO_PR_STATE, run_status=CLEAN_RUN, metrics=True)
        repo = ws / "src" / "repo"
        repo.chmod(0)
        try:
            rc, out = self.run_hook(ws, STUB_FINAL_MSG="I stopped because the endpoint exists.")
        finally:
            repo.chmod(stat.S_IRWXU)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.outcome(ws), "failed/true",
                         "a decline whose snapshot cannot be staged is not a decline")
        self.assertIn("capture hook aborted", self.marker(ws))

    def test_unreadable_snapshot_file_on_a_decline(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        ws = self.new_case(NO_PR_STATE, run_status=CLEAN_RUN, metrics=True)
        locked = ws / "src" / "repo" / "locked.txt"
        locked.write_text("x")
        locked.chmod(0)
        rc, out = self.run_hook(ws, STUB_FINAL_MSG="I stopped because the endpoint exists.")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.outcome(ws), "failed/true", "a partial snapshot is not a decline")
        self.assertIn("repository snapshot could not be staged", self.marker(ws))

    # ---- agent outcomes without a PR: no marker ------------------------------
    def test_no_pr_and_no_final_message(self):
        ws = self.new_case(NO_PR_STATE, run_status=CLEAN_RUN)
        rc, out = self.run_hook(ws)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/false")
        self.assertIsNone(self.marker(ws))
        self.assertEqual(self.regression(ws).get("not_applicable"), True)

    def test_clean_run_with_final_message_is_a_decline(self):
        ws = self.new_case(NO_PR_STATE, run_status=CLEAN_RUN, metrics=True)
        rc, out = self.run_hook(ws, STUB_FINAL_MSG="I stopped because the endpoint already exists.")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "declined/false")
        self.assertIsNone(self.marker(ws))
        self.assertTrue((ws / "judge" / "repo-snapshot" / "README.md").exists(),
                        "the snapshot is staged for the decline judge")
        o = self.js(ws / "output" / "agent-outcome.json")
        self.assertIn("endpoint already exists", o["final_message_excerpt"])
        self.assertIn('"outcome":"declined"', self.raw(ws / "output" / "agent-outcome.json"),
                      "the decline judge's `if:` matches this exact substring")

    def test_timed_out_run_is_a_failure_however_chatty(self):
        ws = self.new_case(NO_PR_STATE, run_status={"exit_code": 124, "timed_out": True},
                           metrics=True)
        rc, out = self.run_hook(ws, STUB_FINAL_MSG="Still working, next I will...")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/false")
        reason = self.js(ws / "output" / "agent-outcome.json")["reason"]
        self.assertIn("exited 124 (timed out)", reason)
        self.assertIsNone(self.marker(ws))

    def test_crashed_run_names_the_exit_without_a_timeout_note(self):
        ws = self.new_case(NO_PR_STATE, run_status={"exit_code": 1, "timed_out": False},
                           metrics=True)
        rc, out = self.run_hook(ws, STUB_FINAL_MSG="Almost there...")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/false")
        reason = self.js(ws / "output" / "agent-outcome.json")["reason"]
        self.assertTrue(reason.endswith("fullsend run exited 1"), reason)

    def test_pr_on_a_non_agent_branch_does_not_count(self):
        state = {"pull_requests": [{"number": 3, "state": "OPEN", "head": "dependabot/npm/x"}]}
        ws = self.new_case(state, run_status=CLEAN_RUN)
        rc, out = self.run_hook(ws)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "failed/false")
        self.assertEqual(self.js(ws / "output" / "pr.json")["pr_found"], False)

    # ---- failures after the outcome: outcome kept, artifacts_incomplete, marker
    def test_pr_head_that_will_not_clone(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_GIT_RC=128)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertTrue(self.incomplete(ws))
        self.assertIn("could not clone the PR head", self.marker(ws))
        self.assertEqual(self.regression(ws).get("clone_failed"), True)
        self.assertFalse((ws / "judge" / "pr-head").exists())
        self.assertIn("Could not clone", self.read_tests_md(ws))
        self.assertTrue((ws / "judge" / "diff.patch").exists(),
                        "the GitHub diff was captured before the clone")

    def test_sandbox_that_will_not_start(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_SANDBOX_STATUS="sandbox", STUB_SANDBOX_RC=125)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertTrue(self.incomplete(ws))
        self.assertIn("sandbox for the regression tests could not be set up", self.marker(ws))
        reg = self.regression(ws)
        self.assertEqual((reg.get("sandbox_failed"), reg.get("exit_code")), (True, 125))
        self.assertTrue((ws / "judge" / "pr-head" / "a.txt").exists())
        self.assertFalse((ws / "output" / "regression.log.status").exists(),
                         "status file consumed")
        self.assertIn("Result: NOT RUN", self.read_tests_md(ws))

    def test_runner_that_dies_before_writing_its_status(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_SANDBOX_NO_STATUS=1)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertEqual(self.regression(ws).get("sandbox_failed"), True)
        self.assertTrue(self.incomplete(ws))
        self.assertIn("could not be set up", self.marker(ws))

    def test_unknown_status_word_is_not_a_result(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_SANDBOX_STATUS="garbled", STUB_SANDBOX_RC=0)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.regression(ws).get("sandbox_failed"), True,
                         "a word the runner never writes is not scored as the command's exit")
        self.assertTrue(self.incomplete(ws))

    def test_pr_head_file_that_will_not_copy(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_GIT_UNREADABLE_FILE=1)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertTrue(self.incomplete(ws),
                        "a judge copy with holes is not graded as the agent's work")
        self.assertIn("could not be copied into judge/pr-head/", self.marker(ws))
        self.assertTrue((ws / "judge" / "pr-head" / "a.txt").exists(), "the rest is still staged")
        self.assertEqual(self.regression(ws).get("exit_code"), 0, "the tests still ran")

    def test_unexpected_error_after_the_outcome(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_GIT_UNREADABLE_DIR=1)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertTrue(self.incomplete(ws))
        self.assertIn("capture hook aborted", self.marker(ws))
        self.assertFalse((ws / "output" / "regression.json").exists())

    def test_diff_github_would_not_return_falls_back_to_the_checkout(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_GH_DIFF_RC=1)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertIsNone(self.marker(ws),
                          "an oversized PR is the agent's PR, not a capture failure")
        self.assertEqual(self.js(ws / "output" / "pr.json").get("diff_source"), "local")
        self.assertIn("hello from the checkout", self.raw(ws / "judge" / "diff.patch"))
        self.assertEqual(self.regression(ws).get("exit_code"), 0, "the tests still ran")

    def test_diff_fails_both_ways(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_GH_DIFF_RC=1, STUB_GIT_DIFF_RC=1)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertTrue(self.incomplete(ws))
        self.assertIn("no diff for the PR", self.marker(ws))
        self.assertEqual(self.js(ws / "output" / "pr.json").get("diff_captured"), False)
        self.assertIn("DIFF CAPTURE FAILED", self.raw(ws / "judge" / "diff.patch"))
        self.assertEqual(self.regression(ws).get("exit_code"), 0,
                         "the tests still ran for the record")

    def test_diff_is_truncated_to_the_limit(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, PR_DIFF_MAX_BYTES=20)
        self.assertEqual(rc, 0, out)
        patch = self.raw(ws / "judge" / "diff.patch")
        self.assertTrue(patch.startswith("diff --git a/a.txt b"), patch)
        self.assertIn("[diff truncated by capture-pr-artifacts.py: 20 of", patch)
        self.assertIsNone(self.marker(ws))

    def test_sigterm_during_the_tests(self):
        ws = self.new_case(PR_STATE)
        t0 = time.monotonic()
        rc, out = self.run_hook(ws, STUB_SANDBOX_TERM=1, STUB_SANDBOX_LINGER=8, timeout=30)
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 143, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertTrue(self.incomplete(ws))
        self.assertIn("SIGTERM", self.marker(ws))
        self.assertFalse((ws / "output" / "regression.json").exists())
        self.assertLess(elapsed, 7,
                        "the marker must not wait for a lingering child; the harness allows 5 s")

    def test_sigkill_during_the_tests_leaves_the_provisional_marker(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_SANDBOX_KILL9=1)
        self.assertEqual(rc, -9)
        self.assertEqual(self.outcome(ws), "pr/false")
        m = self.js(ws / "output" / "infrastructure-failure.json")
        self.assertIn("did not complete", m["reason"])
        self.assertTrue(m.get("provisional"))

    def test_exit_time_check_catches_a_gap_no_handler_saw(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_GIT_REMOVE_PRMD=1)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertTrue(self.incomplete(ws))
        self.assertIn("capture hook ended (exit 0) without judge/pr.md", self.marker(ws))

    # ---- the PR path and the regression classification ----------------------
    def test_pr_path_classification(self):
        cases = (("command", 0, "PASSED (exit 0)"), ("command", 3, "FAILED (exit 3)"),
                 ("command", 125, "FAILED (exit 125)"), ("timeout", 124, "TIMED OUT"),
                 ("lost", 125, "LOST"))
        for status, rc_in, expect in cases:
            with self.subTest(status=status, rc=rc_in):
                ws = self.new_case(PR_STATE)
                rc, out = self.run_hook(ws, STUB_SANDBOX_STATUS=status, STUB_SANDBOX_RC=rc_in)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.outcome(ws), "pr/false")
                self.assertIsNone(self.marker(ws), "a scored result, not a capture failure")
                reg = self.regression(ws)
                self.assertEqual(reg["exit_code"], rc_in)
                self.assertEqual(reg["timed_out"], status == "timeout")
                self.assertEqual(reg["sandbox_lost"], status == "lost")
                self.assertEqual(reg["sandbox_failed"], False)
                self.assertIn(f"Result: {expect}", self.read_tests_md(ws))
                self.assertFalse((ws / "output" / "regression.log.status").exists())
                self.assertTrue((ws / "judge" / "diff.patch").exists())
                self.assertTrue((ws / "judge" / "pr-head" / "a.txt").exists())
                self.assertIn('"outcome":"pr"', self.raw(ws / "output" / "agent-outcome.json"),
                              "the quality judge's `if:` matches this exact substring")

    def test_pr_head_copy_keeps_only_safe_links(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws)
        self.assertEqual(rc, 0, out)
        head = ws / "judge" / "pr-head"
        self.assertTrue((head / "sub" / "b.txt").exists())
        self.assertTrue((head / "ok-link").is_symlink(), "a relative link inside the tree is kept")
        self.assertFalse((head / "evil-link").exists(), "an absolute link is dropped")
        self.assertFalse((head / "escape-link").exists(),
                         "a relative link out of the tree is dropped")
        self.assertFalse((head / "loop").is_symlink(),
                         "a link loop is dropped, not a capture failure")
        self.assertIsNone(self.marker(ws))

    def test_test_output_tail_is_indented_and_sixty_lines(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws, STUB_SANDBOX_LOG_LINES=100)
        self.assertEqual(rc, 0, out)
        md = self.read_tests_md(ws)
        self.assertNotIn("```", md, "a backtick line in test output cannot end the block")
        self.assertIn("    line41\n", md)
        self.assertNotIn("    line40\n", md, "exactly the last 60 lines")
        self.assertIn("    line100\n", md)

    def test_post_script_checklist_is_stripped_from_the_judges_copy(self):
        ws = self.new_case(PR_STATE)
        rc, out = self.run_hook(ws)
        self.assertEqual(rc, 0, out)
        pr_md = self.raw(ws / "judge" / "pr.md")
        self.assertIn("The agent wrote this.", pr_md)
        self.assertNotIn("Post-script verification", pr_md)
        pr = self.js(ws / "output" / "pr.json")
        self.assertIn("Post-script verification", pr["body"], "pr.json keeps the full text")
        self.assertEqual(pr.get("diff_source"), "github")

    def test_no_regression_command_is_skipped_not_failed(self):
        ws = self.new_case(PR_STATE, command="")
        rc, out = self.run_hook(ws)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outcome(ws), "pr/false")
        self.assertIsNone(self.marker(ws))
        self.assertEqual(self.regression(ws).get("skipped"), True)

    def test_usage_without_the_required_environment(self):
        proc = subprocess.run([sys.executable, str(HOOK)], env={"PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("CASE_WORKSPACE is required", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
