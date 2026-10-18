#!/usr/bin/env python3
"""after_each hook (eval/local/code-rhai): save what the judges need from the agent's PR.

Runs before the ephemeral repo is deleted, and runs the repo's own tests on the
PR head. Needs output/fixture-state.json from capture-fixture.sh and (for a
no-PR run) output/run-status.json from run-fullsend.sh.

Environment: CASE_WORKSPACE, CASE_SOURCE_DIR, EPHEMERAL_REPO (required, set by
the harness); GH_TOKEN (the clone and fetch credential); PR_DIFF_MAX_BYTES
(default 400000).

Exit status: 0 complete; 1 a capture failure (the marker below is written);
2 usage; 143 stopped by a signal. The harness swallows it either way.

Writes two trees:

  output/   (read by the Python check judges)
    pr.json            number, url, head, base, title, body, files, +/-,
                       diff_source ("github", or "local" when GitHub would not
                       return the diff and it was computed from the checkout
                       against the base branch) or diff_captured=false when
                       neither worked
    agent-outcome.json {"outcome":"pr"|"declined"|"failed", ...}: "declined" =
                       the agent finished cleanly, opened no PR, and left a
                       final message (its stated reason); the judges branch
                       on this. A capture or fixture failure is "failed" with
                       "infrastructure": true, whatever the agent said, and
                       output/infrastructure-failure.json is written, which
                       run-functional.sh counts as a case failure (the hook's
                       own exit code is swallowed by the harness's
                       continue-on-failure hook mode). A failure after the
                       outcome is known keeps the outcome, adds
                       "artifacts_incomplete": true and writes the same
                       marker; the judges that read judge/ skip or error on
                       it instead of grading the gap. Written compact
                       ("outcome":"pr", no spaces), because the model judges'
                       `if:` conditions in eval.yaml test that substring.
    regression.json    {command, exit_code, timed_out, sandbox_failed,
                       sandbox_lost, ...}: sandbox_failed = the sandbox never
                       ran the command (a capture failure); sandbox_lost = the
                       command started but no single trustworthy status came
                       back (the sandbox stopped answering, more than one
                       status line, a status line from an exec that did not
                       exit cleanly, or a value that is not an exit code; see
                       run-in-sandbox.py), scored as the tests not passing
    regression.log     raw test output (run inside a throwaway OpenShell
                       sandbox by run-in-sandbox.py, never on this host)

  judge/    (staged into the agent judge's working directory, read with tools)
    task.md            the task description the agent was given (input.yaml)
    notes.md           annotations.judge_notes, or "none"
    pr.md              PR title, URL, files changed, the agent's description
    diff.patch         the PR diff (capped at PR_DIFF_MAX_BYTES, default 400000)
    tests.md           regression command, exit code, tail of the log
    pr-head/           full checkout of the PR head, no .git / .venv / node_modules
    agent-final-message.md  the agent's last message (always written)
    repo-snapshot/     on a decline only: the repository the agent was given,
                       so the decline judge can check the stated reason

If no PR exists, pr.json records pr_found=false and the rest is skipped. A
capture failure before the outcome is known leaves no pr.json: in output/,
only the marker and the outcome file. run-review-agent.sh, the next hook,
checks the marker before it looks for pr.json.

The rule this script exists to keep: no capture failure may count against the
agent, and no agent failure may be excused as a capture failure. It is
enforced in one place, Capture.finalize(), which runs on every way out (a
normal end, an exception, SIGTERM from the harness at the hook timeout) and
compares the recorded outcome with the artifacts the judges need for it; a
gap gets the marker. The named failure paths only add a specific reason. A
provisional marker also stands while the regression tests run, so even a
SIGKILL during that step leaves the case marked.

Accepted gaps, both recorded as capture failures although the content is the
agent's: a PR head that git can serve but the host filesystem cannot check out
(a path component over 255 bytes, for one) fails the clone; a PR head large
enough to break the archive, upload or extraction steps of run-in-sandbox.py
fails before its start line.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOOK = "capture-pr-artifacts.py"
POST_SCRIPT_HEADING = "### Post-script verification"
REQUIRED_TOOLS = ("gh", "git", "openshell", "run-in-sandbox.py", "extract-final-message.py")
REF_RE = re.compile(r"[A-Za-z0-9._/-]+")
EXIT_TERM = 143
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
LOG_TAIL_BYTES = 64 * 1024
UTF8 = "utf-8"
# Expanded by git at credential time, as in setup-fixture.sh. The empty value
# first resets the helper list, so a store or keychain helper in the host's
# global config is never asked to save the eval token.
GH_CRED_HELPER = '!f(){ echo "password=${GH_TOKEN}"; };f'
GIT_CRED = ("-c", "credential.helper=", "-c", f"credential.helper={GH_CRED_HELPER}")


class CaptureFailure(Exception):
    """The eval, not the agent, failed.

    Marker plus a failed/infrastructure outcome, or artifacts_incomplete when
    the outcome is already known.
    """


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


def write_json(path: Path, data: dict) -> None:
    """Whole or not at all, compact, and ASCII.

    A half-written file would read as a crashed hook; the model judges' `if:`
    conditions match "outcome":"pr" verbatim; and the harness reads outputs
    with the host's locale encoding, so non-ASCII text is escaped rather than
    written raw.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding=UTF8)
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding=UTF8)


def copy_tree_safely(src: Path, dst: Path, exclude: tuple[str, ...] = ()) -> tuple[int, list[str]]:
    """Copy src to dst; return (files copied, paths that could not be copied).

    Symlinks are kept only when relative and resolving inside src (rsync's
    --safe-links); anything else about a link that cannot be established,
    including a link loop, drops the link silently, so a link in an agent's PR
    head can neither point the judge at a host file nor abort the capture. A
    source directory that cannot be read raises. A file or directory that
    cannot be copied is reported in the second value, and the caller decides
    whether that is a capture failure (the eval's own snapshot) or an
    incomplete judge copy (the PR head).
    """
    count, skipped = 0, []
    root = os.path.realpath(src)

    def unreadable(err: OSError):
        raise err

    for dirpath, dirnames, filenames in os.walk(src, followlinks=False, onerror=unreadable):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        rel = Path(dirpath).relative_to(src)
        try:
            (dst / rel).mkdir(parents=True, exist_ok=True)
        except OSError as err:
            skipped.append(f"{rel}/: {err}")
            dirnames[:] = []
            continue
        linked_dirs = [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
        for name in filenames + linked_dirs:
            s, d = Path(dirpath) / name, dst / rel / name
            try:
                if s.is_symlink():
                    target = os.readlink(s)
                    resolved = os.path.realpath(os.path.join(dirpath, target))
                    outside = os.path.commonpath([root, resolved]) != root
                    if os.path.isabs(target) or outside or os.path.islink(resolved):
                        continue  # absolute, escaping, or a loop (realpath stops on one)
                    os.symlink(target, d)
                else:
                    shutil.copy2(s, d, follow_symlinks=False)
                count += 1
            except (OSError, RuntimeError, ValueError) as err:
                if s.is_symlink():
                    continue  # an unsafe or unresolvable link is dropped
                skipped.append(f"{rel / name}: {err}")
    # The copy lacks what was excluded or dropped, so a link that was safe in
    # the source can dangle here, and its literal target could point a lexical
    # resolver outside the copy. Keep only links that resolve inside the copy.
    droot = os.path.realpath(dst)
    for dirpath, dirnames, filenames in os.walk(dst, followlinks=False):
        for name in filenames + dirnames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                target = os.path.realpath(p)
                if not os.path.exists(target) or os.path.commonpath([droot, target]) != droot:
                    os.unlink(p)
                    count -= 1
    return count, skipped


def cap_file(path: Path, max_bytes: int, note: str) -> int:
    """Truncate path to max_bytes plus a note; return the original size."""
    size = path.stat().st_size
    if size > max_bytes:
        with path.open("r+b") as fh:
            fh.truncate(max_bytes)
            fh.seek(0, os.SEEK_END)
            fh.write(f"\n\n{note.format(shown=max_bytes, size=size)}\n".encode())
    return size


def on_path(tool: str) -> bool:
    return shutil.which(tool) is not None


def check_ref(kind: str, ref: str) -> str:
    # A ref reaches git as an argument; it must not look like an option or a
    # force refspec.
    if not REF_RE.fullmatch(ref) or ref[0] in "-+":
        raise CaptureFailure(f"unexpected {kind}: {ref}")
    return ref


def tail_text(path: Path, lines: int) -> str:
    """The last `lines` lines of a possibly huge file, reading only its tail."""
    if not path.exists():
        return ""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - LOG_TAIL_BYTES))
        chunk = fh.read()
    return b"\n".join(chunk.rstrip(b"\n").split(b"\n")[-lines:]).decode(errors="replace")


class Capture:
    def __init__(self, env: dict[str, str]):
        self.workspace = Path(env["CASE_WORKSPACE"])
        self.source = Path(env["CASE_SOURCE_DIR"])
        self.repo = env["EPHEMERAL_REPO"]
        self.env = env
        self.gh_token = env.get("GH_TOKEN", "")
        self.output = self.workspace / "output"
        self.judge = self.workspace / "judge"
        self.state_file = self.output / "fixture-state.json"
        self.marker = self.output / "infrastructure-failure.json"
        self.outcome_file = self.output / "agent-outcome.json"
        self.output.mkdir(parents=True, exist_ok=True)
        self.judge.mkdir(parents=True, exist_ok=True)
        self.outcome: str | None = None
        self.final_msg = ""
        self.run_dir: Path | None = None
        self.work: Path | None = None
        self.child: subprocess.Popen | None = None
        self.terminated = False

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr)

    # ---- the files every failure path ends in ---------------------------------
    def write_marker(self, reason: str, provisional: bool = False) -> None:
        data = {"reason": reason, "hook": HOOK,
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if provisional:
            data["provisional"] = True
        write_json(self.marker, data)

    def write_outcome(self, outcome: str, reason: str, infrastructure: bool = False) -> None:
        self.outcome = outcome
        write_json(self.outcome_file, {
            "outcome": outcome, "reason": reason, "infrastructure": infrastructure,
            "final_message_chars": len(self.final_msg),
            "final_message_excerpt": self.final_msg[:400],
        })
        self.log(f"outcome: {outcome}{', infrastructure' if infrastructure else ''} ({reason})")

    def artifacts_incomplete(self, reason: str) -> None:
        """A failure after the outcome is known.

        Writes the marker plus the flag the judges' conditions read, so the gap
        is not graded as the agent's doing.
        """
        self.write_marker(reason)
        current = read_json(self.outcome_file) or {}
        current.update({"artifacts_incomplete": True, "artifacts_incomplete_reason": reason})
        write_json(self.outcome_file, current)
        self.log(f"outcome kept; {reason}")

    def record_eval_failure(self, reason: str) -> None:
        if self.outcome is not None or self.outcome_file.exists():
            self.artifacts_incomplete(reason)
        else:
            self.write_marker(reason)
            self.write_outcome("failed", reason, infrastructure=True)

    # ---- helpers ----------------------------------------------------------------
    def run_child(self, argv: list[str], **kw) -> subprocess.CompletedProcess:
        """subprocess.run with stdin closed.

        The child shares our process group so the harness's group-wide SIGTERM
        reaches it too. On Terminated the child stays in self.child for
        cleanup_child().
        """
        proc = self.child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **kw)
        out, err = proc.communicate()
        self.child = None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def cleanup_child(self, grace: float = 3.0) -> None:
        """Give an interrupted child a moment to finish its own cleanup, then kill it.

        After the harness's group SIGTERM the child has already been signalled;
        sending a second TERM would only interrupt its cleanup, so none is sent.
        """
        if self.child is None or self.child.poll() is not None:
            return
        try:
            if not self.terminated:
                self.child.terminate()
            self.child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.child.kill()
        except OSError:
            pass

    def git_env(self) -> dict[str, str]:
        # No tty prompt: a missing token fails fast instead of waiting for input.
        return {**self.env, "GH_TOKEN": self.gh_token, "GIT_TERMINAL_PROMPT": "0"}

    def max_diff_bytes(self) -> int:
        raw = self.env.get("PR_DIFF_MAX_BYTES", "400000")
        if not re.fullmatch(r"[0-9]+", raw):
            raise CaptureFailure(f"PR_DIFF_MAX_BYTES must be a whole number of bytes, got '{raw}'")
        return int(raw)

    def annotations(self) -> dict:
        return load_yaml(self.source / "annotations.yaml")

    # ---- steps ------------------------------------------------------------------
    def check_tools(self) -> None:
        missing = [t for t in REQUIRED_TOOLS if not on_path(t)]
        if not have_pyyaml() and not on_path("yq"):
            missing.append("yq (or the PyYAML module)")
        if missing:
            raise CaptureFailure("required tools not found in PATH: " + " ".join(missing))

    def write_task_and_notes(self) -> None:
        # Always written, so the judge can read them even when the run produced
        # nothing to grade.
        fixture = load_yaml(self.source / "input.yaml").get("fixture") or {}
        write_text(self.judge / "task.md",
                   "# Task description given to the agent\n\n## Title\n\n"
                   f"{fixture.get('title', '')}\n\n## Body\n\n{fixture.get('body', '')}\n")
        notes = str(self.annotations().get("judge_notes") or "").strip()
        write_text(self.judge / "notes.md",
                   "# Notes from the case author\n\n"
                   "Things the task leaves open, or that must not be held against the agent.\n\n"
                   f"{notes or 'none'}\n")

    def read_final_message(self) -> None:
        # The agent's final message, whatever the runtime (extract-final-message.py
        # understands the Claude Code, pi and codex transcript shapes). An
        # extractor that fails is a capture failure, not "no final message": it
        # prints nothing and exits 0 when the transcript holds none.
        run_dirs = sorted(p for p in self.output.glob("fs-cod-*") if p.is_dir())
        self.run_dir = run_dirs[0] if run_dirs else None
        if self.run_dir is not None:
            res = self.run_child(["extract-final-message.py", str(self.run_dir)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                raise CaptureFailure(f"final-message extractor exited {res.returncode}: "
                                     f"{res.stderr.strip()[-400:]}")
            self.final_msg = res.stdout.strip()
        write_text(self.judge / "agent-final-message.md",
                   "# The agent's final message\n\n"
                   f"{self.final_msg or '(no final message found in the transcript)'}\n")

    def no_pr_files(self, reason: str) -> None:
        write_json(self.output / "pr.json", {"pr_found": False, "reason": reason})
        write_text(self.judge / "pr.md",
                   f"# Pull request\n\nNo pull request was produced: {reason}\n")
        # No PR head to test: say so explicitly, so the regression check can
        # report "not applicable" instead of mistaking a missing file for a
        # failed capture.
        write_json(self.output / "regression.json",
                   {"command": None, "exit_code": None, "not_applicable": True,
                    "reason": "no PR: " + reason})
        write_text(self.judge / "tests.md",
                   "# Repository tests\n\nNot applicable: no pull request was produced.\n")

    def no_pr(self, reason: str) -> None:
        self.no_pr_files(reason)
        # Declined = the run finished CLEANLY (fullsend exited 0, metrics.json
        # exists) and the agent said why. A run that timed out or crashed is
        # "failed" however chatty its last message was. run-status.json is
        # written by run-fullsend.sh after `fullsend run` returns; its absence
        # means the runner script itself died, never anything the agent did.
        status = read_json(self.output / "run-status.json")
        if not isinstance(status, dict) or not isinstance(status.get("exit_code"), int):
            raise CaptureFailure(f"{reason}; run status not recorded "
                                 "(run-status.json missing or unreadable)")
        rc = status["exit_code"]
        if rc != 0:
            timed_out = " (timed out)" if status.get("timed_out") else ""
            self.write_outcome("failed", f"{reason}; fullsend run exited {rc}{timed_out}")
            return
        metrics = self.run_dir is not None and (self.run_dir / "metrics.json").exists()
        if self.final_msg and metrics:
            # Stage the repository the agent was given so the decline judge can
            # verify claims like "this endpoint already exists"; staged before
            # the outcome is written, so a failure here is a capture failure.
            _, skipped = copy_tree_safely(self.source / "repo", self.judge / "repo-snapshot",
                                          exclude=(".git",))
            if skipped:
                raise CaptureFailure(f"{len(skipped)} file(s) of the repository snapshot could "
                                     f"not be staged for the decline judge: {skipped[0]}")
            self.write_outcome("declined", reason)
            self.log(f"declined (final message {len(self.final_msg)} chars); staged repo-snapshot/")
            return
        self.write_outcome("failed", reason)

    def pick_pr(self) -> dict | None:
        if not self.state_file.exists():
            raise CaptureFailure("fixture-state.json missing; capture-fixture.sh did not run")
        # The repo name reaches gh and a clone URL; run-fullsend.sh checks the same shape.
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", self.repo):
            raise CaptureFailure(f"unexpected EPHEMERAL_REPO: {self.repo}")
        state = read_json(self.state_file)
        if not isinstance(state, dict):
            raise CaptureFailure("fixture-state.json is not valid JSON")
        # capture-fixture.sh records an empty PR list when GitHub would not
        # return one; that is not "no PR", it is a capture failure.
        if state.get("pull_requests_error"):
            raise CaptureFailure(f"PR list not captured: {state['pull_requests_error']}")
        # Only the agent's own PR counts: fullsend's post-script pushes
        # agent/<issue>-<slug> branches. Anything else on the throwaway repo
        # (Dependabot, for one) is ignored.
        for pr in state.get("pull_requests") or []:
            open_or_merged = str(pr.get("state", "")).upper() in ("OPEN", "MERGED")
            if open_or_merged and str(pr.get("head", "")).startswith("agent/"):
                return pr
        return None

    def pr_metadata(self, num: int) -> dict:
        fields = ("number,url,title,body,headRefName,baseRefName,headRefOid,"
                  "files,additions,deletions")
        res = self.run_child(["gh", "pr", "view", str(num), "--repo", self.repo, "--json", fields],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise CaptureFailure(f"gh pr view {num} exited {res.returncode}: "
                                 f"{res.stderr.strip()[-400:]}")
        raw = json.loads(res.stdout)
        pr = {"pr_found": True, "number": raw.get("number"), "url": raw.get("url"),
              "title": raw.get("title"), "body": raw.get("body"), "head": raw.get("headRefName"),
              "base": raw.get("baseRefName"), "head_sha": raw.get("headRefOid"),
              "additions": raw.get("additions"), "deletions": raw.get("deletions"),
              "files": [f.get("path") for f in raw.get("files") or []]}
        write_json(self.output / "pr.json", pr)
        # fullsend's post-script appends a "Post-script verification" checklist
        # to every PR it opens. That is pipeline boilerplate, identical on
        # every PR, so it is dropped from the judge's copy; pr.json keeps it.
        body = pr["body"] or "(empty)"
        if POST_SCRIPT_HEADING in body:
            body = body.split(POST_SCRIPT_HEADING, 1)[0].rstrip()
        files = "\n".join(f"- {f}" for f in pr["files"])
        write_text(self.judge / "pr.md",
                   f"# Pull request\n\nTitle: {pr['title']}\nURL: {pr['url']}\n"
                   f"Branch: {pr['head']} -> {pr['base']}\n"
                   f"Files changed: {len(pr['files'])} "
                   f"(+{pr['additions']} / -{pr['deletions']})\n\n"
                   f"{files}\n\n## Description written by the agent\n\n{body}\n")
        return pr

    def update_pr_json(self, **fields) -> None:
        pr = read_json(self.output / "pr.json") or {}
        pr.update(fields)
        write_json(self.output / "pr.json", pr)

    def finish_diff(self, source: str) -> None:
        # Size-capped so the judge's reading stays bounded; the full change is
        # in pr-head/.
        size = cap_file(self.judge / "diff.patch", self.max_diff_bytes(),
                        f"[diff truncated by {HOOK}: {{shown}} of {{size}} bytes shown; "
                        "the full change is in pr-head/]")
        self.update_pr_json(diff_source=source)
        self.log(f"Saved PR diff ({size} bytes, {source})")

    def fetch_diff(self, num: int) -> bool:
        # Streamed to the file, never held in memory: the fallback below exists
        # for diffs too large for GitHub to serve.
        with (self.judge / "diff.patch").open("wb") as fh:
            res = self.run_child(["gh", "pr", "diff", str(num), "--repo", self.repo],
                           stdout=fh, stderr=subprocess.PIPE)
        if res.returncode != 0:
            (self.judge / "diff.patch").unlink(missing_ok=True)
            self.log(f"WARNING: gh pr diff exited {res.returncode}; "
                     "will diff the checkout against its base instead")
            return False
        self.finish_diff("github")
        return True

    def local_diff(self, work_pr: Path, base_ref: str) -> None:
        # GitHub refuses the diff of a very large PR (HTTP 406), which an agent
        # can produce by committing a vendored tree. That is the agent's PR,
        # not a capture failure, so the diff is computed from the checkout. The
        # fixture repo is private, so the fetch carries the same credential as
        # the clone (a -c helper is not persisted in the clone's config).
        git = ["git", *GIT_CRED, "-C", str(work_pr)]
        fetch = self.run_child([*git, "fetch", "--quiet", "origin", base_ref], env=self.git_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        detail = fetch.stdout.decode(errors="replace")
        if fetch.returncode == 0:
            with (self.judge / "diff.patch").open("wb") as fh:
                diff = self.run_child([*git, "diff", "FETCH_HEAD...HEAD"], env=self.git_env(),
                                stdout=fh, stderr=subprocess.PIPE)
            if diff.returncode == 0:
                self.finish_diff("local")
                return
            detail = diff.stderr.decode(errors="replace")
        # Never hand the judge an empty diff that looks like a real one.
        write_text(self.judge / "diff.patch",
                   "[DIFF CAPTURE FAILED: gh pr diff failed, and so did git diff against "
                   f"{base_ref} in the checkout. Judge the change from pr-head/ only.]\n{detail}\n")
        self.update_pr_json(diff_captured=False)
        self.artifacts_incomplete(f"no diff for the PR: gh pr diff failed, and git diff against "
                                  f"{base_ref} failed in the checkout")

    def clone(self, head_ref: str) -> Path | None:
        # Checkout of the PR head for the judge to read around the change.
        # GitHub occasionally resets a clone (run-fullsend.sh retries for the
        # same reason); three attempts.
        self.work = Path(tempfile.mkdtemp(prefix="capture-pr-"))
        dest = self.work / "pr"
        for attempt in (1, 2, 3):
            shutil.rmtree(dest, ignore_errors=True)
            res = self.run_child(["git", *GIT_CRED, "clone", "--quiet", "--branch", head_ref,
                            f"https://x-access-token@github.com/{self.repo}.git", str(dest)],
                           env=self.git_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if res.returncode == 0:
                return dest
            self.log(f"WARNING: clone attempt {attempt} failed (exit {res.returncode})")
            if attempt < 3:
                time.sleep(attempt)
        return None

    def regression(self, work_pr: Path, command: str, timeout_s: object) -> None:
        reg = self.output / "regression.json"
        tests_md = self.judge / "tests.md"
        if not command or command == "TODO":
            write_json(reg, {"command": None, "exit_code": None, "skipped": True,
                             "reason": "annotations.regression_tests.command not set"})
            write_text(tests_md, "# Repository tests\n\n"
                                 "Not run: no regression command is set for this case.\n")
            return
        self.log(f"Running regression tests: {command}")
        log = self.output / "regression.log"
        status_file = log.with_name(log.name + ".status")
        # The marker stands while the tests run, so a hook killed outright in
        # this step (SIGKILL after the harness's grace) still leaves the case
        # marked; it is removed once the result is on file. It never replaces
        # a marker a named path already wrote.
        provisional = not self.marker.exists()
        if provisional:
            self.write_marker("regression tests did not complete: the capture hook was stopped "
                              "while they ran", provisional=True)
        # The PR head is agent-written code built from untrusted inputs. It
        # runs in a throwaway OpenShell sandbox (the coder's image, registry-
        # only network, no credentials), not on this host.
        rc = self.run_child(["run-in-sandbox.py", str(work_pr), command, str(timeout_s),
                       str(log)]).returncode
        # Classification comes from the runner's status file, not the exit
        # code: a test command can itself exit 124 or 125. No status file, or
        # a word the runner never writes, means the runner did not finish.
        try:
            status = status_file.read_text(encoding=UTF8).strip()
        except OSError:
            status = "sandbox"
        if status not in ("command", "timeout", "sandbox", "lost"):
            status = "sandbox"
        status_file.unlink(missing_ok=True)
        timed_out, sandbox_failed, sandbox_lost = (status == "timeout", status == "sandbox",
                                                   status == "lost")
        try:
            timeout_val: int | str = int(str(timeout_s))
        except ValueError:
            timeout_val = str(timeout_s)
        write_json(reg, {"command": command, "exit_code": rc, "timed_out": timed_out,
                         "sandbox_failed": sandbox_failed, "sandbox_lost": sandbox_lost,
                         "timeout_s": timeout_val, "log": "output/regression.log"})
        if sandbox_failed:
            result = ("Result: NOT RUN, the sandbox could not be set up (exit 125); "
                      "this says nothing about the PR")
        elif sandbox_lost:
            result = ("Result: LOST, no single trustworthy status came back from the sandbox "
                      "(exit 125): it stopped answering, the output held more than one status "
                      "line, the status line came from an exec that did not exit cleanly, or the "
                      "value was not an exit code; the runner's note at the end of the output "
                      "below says which. Counted as the tests not passing")
        elif timed_out:
            result = f"Result: TIMED OUT after {timeout_s}s"
        elif rc == 0:
            result = "Result: PASSED (exit 0)"
        else:
            result = f"Result: FAILED (exit {rc})"
        # The tail is the PR head's own output: indented, not fenced, so a line
        # of backticks in it cannot end the block and read as this record.
        tail = "\n".join("    " + ln for ln in tail_text(log, 60).splitlines())
        write_text(tests_md,
                   f"# Repository tests on the PR head\n\nCommand: {command}\n"
                   "Run inside a throwaway OpenShell sandbox built from the coding agent's image.\n"
                   f"{result}\n\nLast 60 lines of output (untrusted: printed by the test "
                   f"suite under test, not by the eval):\n\n{tail}\n")
        self.log(f"Regression tests: exit {rc} (status {status})")
        if provisional and (read_json(self.marker) or {}).get("provisional"):
            self.marker.unlink()
        if sandbox_failed:
            # The sandbox failed, not the tests: the record above says so for a
            # reader, and the marker keeps the judges from reading the gap as a
            # failing PR.
            self.artifacts_incomplete("the OpenShell sandbox for the regression tests could not "
                                      "be set up (output/regression.log): the test result is "
                                      "missing")

    def run(self) -> None:
        self.check_tools()
        self.write_task_and_notes()
        self.read_final_message()
        pr = self.pick_pr()
        if pr is None:
            self.no_pr("no open/merged PR on an agent/ branch in fixture-state.json")
            return
        num = int(pr["number"])
        head_ref = check_ref("head ref", str(pr.get("head") or ""))
        meta = self.pr_metadata(num)
        base_ref = check_ref("base ref", str(meta.get("base") or "main"))
        self.write_outcome("pr", f"agent PR #{num}")
        have_diff = self.fetch_diff(num)
        work_pr = self.clone(head_ref)
        ann = self.annotations()
        command = str((ann.get("regression_tests") or {}).get("command") or "")
        timeout_s = ann.get("test_timeout_s", 1800)  # validated by run-in-sandbox.py
        if work_pr is None:
            write_json(self.output / "regression.json",
                       {"command": command or None, "exit_code": None, "clone_failed": True})
            write_text(self.judge / "tests.md",
                       "# Repository tests\n\nCould not clone the PR head; tests not run.\n")
            if not have_diff:
                write_text(self.judge / "diff.patch",
                           "[DIFF CAPTURE FAILED: gh pr diff failed and the PR head would not "
                           "clone.]\n")
                self.update_pr_json(diff_captured=False)
            # No checkout: no pr-head/ for the quality judge and no test result.
            self.artifacts_incomplete(f"could not clone the PR head {head_ref} of {self.repo}: "
                                      "judge/pr-head/ and the test result are missing")
            return
        n, skipped = copy_tree_safely(work_pr, self.judge / "pr-head",
                                      exclude=(".git", ".venv", "node_modules"))
        self.log(f"Saved PR head checkout -> {self.judge / 'pr-head'} ({n} files)")
        if skipped:
            # The judge would read a tree with holes as the agent's work.
            self.artifacts_incomplete(f"{len(skipped)} file(s) of the PR head could not be "
                                      f"copied into judge/pr-head/: {skipped[0]}")
        if not have_diff:
            self.local_diff(work_pr, base_ref)
        self.regression(work_pr, command, timeout_s)

    # ---- the check every route to the end passes through ----------------------
    def missing_artifacts(self) -> str:
        """What the recorded outcome lacks; empty when complete."""
        current = read_json(self.outcome_file)
        if current is None:
            return "an outcome (agent-outcome.json was never written or is unreadable)"
        outcome = current.get("outcome")
        missing: list[str] = []
        if outcome == "pr":
            pr = read_json(self.output / "pr.json") or {}
            if not pr:
                missing.append("output/pr.json")
            if pr.get("diff_captured") is False or not (self.judge / "diff.patch").exists():
                missing.append("judge/diff.patch")
            for name in ("pr.md", "tests.md"):
                if not (self.judge / name).exists():
                    missing.append(f"judge/{name}")
            if not (self.judge / "pr-head").is_dir():
                missing.append("judge/pr-head/")
            reg = read_json(self.output / "regression.json")
            if reg is None:
                missing.append("output/regression.json")
            elif reg.get("clone_failed") or reg.get("sandbox_failed"):
                missing.append("a test result (the PR head was not cloned, or the sandbox "
                               "never ran the tests)")
        elif outcome == "declined":
            if not (self.judge / "repo-snapshot").is_dir():
                missing.append("judge/repo-snapshot/")
        elif outcome != "failed":
            missing.append(f"a readable outcome (agent-outcome.json says {outcome!r})")
        return ", ".join(missing)

    def finalize(self, rc: int) -> int:
        # Everything from here is bounded and must run to completion, whatever
        # path led here, so a signal landing now is ignored.
        for sig in SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        self.cleanup_child()
        if self.work is not None:
            shutil.rmtree(self.work, ignore_errors=True)
        if not self.marker.exists():
            missing = self.missing_artifacts()
            if missing:
                self.record_eval_failure(f"capture hook ended (exit {rc}) without {missing}")
        # A marker means the hook did not finish cleanly; its exit status says so too.
        if self.marker.exists() and rc == 0:
            rc = 1
        return rc


def main() -> int:
    for var in ("CASE_WORKSPACE", "CASE_SOURCE_DIR", "EPHEMERAL_REPO"):
        if not os.environ.get(var):
            print(f"{HOOK}: {var} is required", file=sys.stderr)
            return 2
    cap = Capture(dict(os.environ))
    for sig in SIGNALS:
        signal.signal(sig, _on_signal)
    rc = 0
    try:
        cap.run()
    except CaptureFailure as e:
        cap.record_eval_failure(str(e))
        rc = 1
    except Terminated as t:
        # The harness hook timeout, most likely. A repeat must not interrupt
        # the marker or the cleanup, so further signals are ignored from here.
        for sig in SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        cap.terminated = True
        cap.record_eval_failure(f"capture hook stopped by {t} before it finished")
        rc = EXIT_TERM
    except Exception as e:  # anything unanticipated is a capture failure
        cap.record_eval_failure(f"capture hook aborted: {type(e).__name__}: {e}")
        rc = 1
    finally:
        rc = cap.finalize(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
