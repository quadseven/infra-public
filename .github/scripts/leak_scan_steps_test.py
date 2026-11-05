#!/usr/bin/env python3
"""Runs the PR fetch and issue-comment steps of _reusable.leak-scan.yml under bash.

On an `issues` event the leak scan has no check to fail, so step 9 tells the
filer through a comment, and each comment is an email. The test lifts that
step's `run:` script out of the workflow file as it is on disk and runs it
against a fake `gh` that serves canned comments and records every write, the
same approach as deploy_k8s_steps_test.py.

Python stdlib only (unittest), plus bash and jq (both on the runner).

Run: python3 .github/scripts/leak_scan_steps_test.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "_reusable.leak-scan.yml"
SCANNER = Path(__file__).resolve().parent / "check_private_leaks.py"
STEP_RE = re.compile(r"^(\s*)- name:\s*(.+?)\s*$")


def step(prefix: str) -> tuple[str, str]:
    """The `if:` line and dedented `run: |` block of the step whose name
    starts with `prefix`, so a renamed step is still found by its number."""
    lines = WORKFLOW.read_text().splitlines()
    for i, line in enumerate(lines):
        m = STEP_RE.match(line)
        if not m or not m.group(2).strip("\"'").startswith(prefix):
            continue
        step_indent = len(m.group(1))
        cond = ""
        for j in range(i + 1, len(lines)):
            cur = lines[j]
            if cur.strip() and len(cur) - len(cur.lstrip()) <= step_indent:
                break
            if cur.strip().startswith("if:"):
                cond = cur.strip()[3:].strip()
            if re.match(r"^\s*run:\s*\|\s*$", cur):
                key_indent = len(cur) - len(cur.lstrip())
                body = []
                for k in range(j + 1, len(lines)):
                    nxt = lines[k]
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= key_indent:
                        break
                    body.append(nxt)
                indent = min(len(b) - len(b.lstrip()) for b in body if b.strip())
                return cond, "\n".join(b[indent:] for b in body) + "\n"
        raise AssertionError(f"step {prefix!r} has no `run: |` block")
    raise AssertionError(f"no step starting {prefix!r} in {WORKFLOW.name}")


FAKE_GH = r"""#!/usr/bin/env bash
# Logs each call; a GET of the comments list prints the canned comments.
printf '%s\0' "$@" >> "$GH_LOG"; printf '\036' >> "$GH_LOG"
if [ "$1" = api ] && [[ " $* " != *" -X "* ]] && [[ "$*" == *"/comments"* ]]; then
  if [ -n "${GH_LIST_FAILS:-}" ]; then echo "HTTP 502" >&2; exit 1; fi
  cat "$GH_COMMENTS"
fi
"""

COMMENT_STEP = "9."
BOT = "github-actions[bot]"
FINDING_OUTPUT = """BLOCKED: private infrastructure identifiers found (9 shape patterns)

  {path}:1  [private-issue-ref] 'other-repo#N' - a cross-repo issue reference (may name a private repo)
  {path}:4  [deny-list] <redacted, 6 chars> - a deny-listed term

This repo is PUBLIC.
"""


# Never skipped in CI (CI=true on every Actions runner): a skip there would
# report green over tests that did not run.
@unittest.skipIf(shutil.which("jq") is None and not os.environ.get("CI"), "jq is on the runner; install it to run this here")
class IssueComment(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="leak-scan-step-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.log = self.tmp / "gh.log"
        self.log.write_text("")
        self.comments = self.tmp / "comments.json"
        self.scan = self.tmp / "text-scan.txt"
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"

    def run_step(self, worst: str, comments: list[dict], scan_output: str = "", **env: str) -> list[list[str]]:
        done, writes = self.attempt(worst, comments, scan_output, **env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return writes

    def attempt(
        self, worst: str, comments: list[dict], scan_output: str = "", **env: str
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        self.comments.write_text(json.dumps(comments))
        self.scan.write_text(scan_output.format(path=self.tmp / "text.md"))
        _, script = step(COMMENT_STEP)
        done = subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", script],
            env={
                "PATH": self.path,
                "GH_LOG": str(self.log),
                "GH_COMMENTS": str(self.comments),
                "GH_TOKEN": "unused",
                "REPO": "owner/public-repo",
                "ISSUE_NUMBER": "7",
                "WORST": worst,
                "SCAN_OUTPUT": str(self.scan),
                "RUN_URL": "https://github.com/owner/public-repo/actions/runs/1",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        calls = [c.split("\0")[:-1] for c in self.log.read_text().split("\036") if c]
        return done, [c for c in calls if not (c[0] == "api" and "-X" not in c)]

    def body_of(self, call: list[str]) -> str:
        for i, arg in enumerate(call):
            if arg == "--body":
                return call[i + 1]
            if arg.startswith("body="):
                return arg[len("body="):]
        raise AssertionError(f"no body in {call}")

    def test_the_comment_names_each_flagged_line_and_rule(self):
        writes = self.run_step("1", [], FINDING_OUTPUT)
        self.assertEqual(len(writes), 1, writes)
        body = self.body_of(writes[0])
        self.assertIn("title: private-issue-ref\n", body)
        self.assertIn("body line 3: deny-list\n", body)
        self.assertIn("https://github.com/owner/public-repo/actions/runs/1", body)

    def test_the_comment_never_repeats_the_matched_text(self):
        body = self.body_of(self.run_step("1", [], FINDING_OUTPUT)[0])
        self.assertNotIn("other-repo", body)
        self.assertNotIn("redacted", body)

    def test_the_real_scanner_output_is_read_line_by_line(self):
        # Built at run time so this file's own diff carries no reference.
        ref = "other" + "-repo#" + "12"
        text = self.tmp / "text.md"
        text.write_text(f"A title\nfirst line\nsee {ref}\n")
        scanned = subprocess.run(
            ["python3", str(SCANNER), "--text-file", str(text), "--allow-repo-ref", "owner/public-repo"],
            # Both streams, as step 8's `2>&1` keeps them: findings go to stderr.
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False,
        )
        self.assertEqual(scanned.returncode, 1, scanned.stdout)
        body = self.body_of(self.run_step("1", [], scanned.stdout.replace("{", "{{").replace("}", "}}"))[0])
        self.assertIn("body line 2: private-issue-ref\n", body)
        self.assertNotIn(ref, body)

    def test_step_8_keeps_both_scanner_streams_for_step_9(self):
        _, script = step("8.")
        self.assertRegex(script, r'--text-file "\$RUNNER_TEMP/text.md".*> "\$RUNNER_TEMP/text-scan.txt" 2>&1')
        self.assertIn('echo "textscan=$RUNNER_TEMP/text-scan.txt"', script)

    def test_a_failed_comment_listing_fails_the_step_and_posts_nothing(self):
        done, writes = self.attempt("1", [], FINDING_OUTPUT, GH_LIST_FAILS="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(writes, [])

    def test_a_second_finding_edits_the_earlier_comment_instead_of_mailing_again(self):
        earlier = {"id": 55, "user": {"login": BOT}, "body": "<!-- leak-scan:issue-text -->\nold"}
        writes = self.run_step("1", [earlier], FINDING_OUTPUT)
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0][:4], ["api", "-X", "PATCH", "repos/owner/public-repo/issues/comments/55"])

    def test_an_older_unmarked_warning_is_reused_too(self):
        legacy = {"id": 41, "user": {"login": BOT}, "body": ":warning: **Leak scan found a possible thing.**"}
        writes = self.run_step("1", [legacy], FINDING_OUTPUT)
        self.assertEqual(writes[0][:4], ["api", "-X", "PATCH", "repos/owner/public-repo/issues/comments/41"])

    def test_a_clean_rescan_removes_the_warning(self):
        earlier = {"id": 55, "user": {"login": BOT}, "body": "<!-- leak-scan:issue-text -->\nold"}
        writes = self.run_step("0", [earlier], "leak guard: clean\n")
        self.assertEqual(writes, [["api", "-X", "DELETE", "repos/owner/public-repo/issues/comments/55"]])

    def test_a_clean_scan_with_no_warning_writes_nothing(self):
        self.assertEqual(self.run_step("0", [], "leak guard: clean\n"), [])

    def test_a_person_quoting_the_marker_is_left_alone(self):
        quoted = {"id": 9, "user": {"login": "someone"}, "body": "<!-- leak-scan:issue-text -->\nmine"}
        writes = self.run_step("0", [quoted], "leak guard: clean\n")
        self.assertEqual(writes, [])

    def test_the_step_runs_on_clean_and_finding_issue_scans_but_not_on_scan_failure(self):
        cond, _ = step(COMMENT_STEP)
        self.assertIn("github.event_name == 'issues'", cond)
        self.assertIn("steps.scan.outputs.worst == '0'", cond)
        self.assertIn("steps.scan.outputs.worst == '1'", cond)


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, timeout=30, check=True,
    )
    return done.stdout.strip()


FETCH_STEP = "7b."


class SquashMergedPrDiff(unittest.TestCase):
    """An edit to a PR after a squash merge and source-branch deletion
    (a consumer hit exit 128 here). The squash commit is new, so the old head is
    reachable only through the retained `refs/pull/<n>/head`; the clone that
    actions/checkout makes (every branch and tag) does not hold it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="leak-scan-squash-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def build(self, feature_line: str, keep_pull_ref: bool = True) -> tuple[str, str]:
        work = self.tmp / "work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        (work / "a.txt").write_text("base\n")
        git(work, "add", "a.txt")
        git(work, "commit", "-q", "-m", "base")
        base = git(work, "rev-parse", "HEAD")
        git(work, "checkout", "-q", "-b", "feature")
        (work / "a.txt").write_text("base\n" + feature_line + "\n")
        git(work, "commit", "-q", "-am", "feature")
        head = git(work, "rev-parse", "HEAD")
        git(work, "checkout", "-q", "main")
        git(work, "merge", "-q", "--squash", "feature")
        git(work, "commit", "-q", "-m", "squash")
        origin = self.tmp / "origin.git"
        git(self.tmp, "clone", "-q", "--bare", str(work), str(origin))
        if keep_pull_ref:
            git(origin, "update-ref", "refs/pull/7/head", head)
        git(origin, "branch", "-D", "feature")
        git(origin, "reflog", "expire", "--expire=now", "--all")
        git(origin, "gc", "-q", "--prune=now")
        # What checkout with fetch-depth 0 leaves: every branch and tag.
        git(self.tmp, "clone", "-q", "--no-local", str(origin), "_repo")
        missing = subprocess.run(
            ["git", "cat-file", "-e", head + "^{commit}"],
            cwd=self.tmp / "_repo", capture_output=True, check=False,
        )
        self.assertNotEqual(missing.returncode, 0, "the clone must not already hold the old head")
        return base, head

    def run_script(self, prefix: str, **env: str) -> subprocess.CompletedProcess[str]:
        _, script = step(prefix)
        return subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", script],
            cwd=self.tmp,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp), **env},
            capture_output=True, text=True, timeout=60, check=False,
        )

    def fetch(self, base: str, head: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(FETCH_STEP, BASE_SHA=base, HEAD_SHA=head, PR_NUMBER="7", FETCH_TOKEN="")

    def scan(self, base: str, head: str) -> str:
        out = self.tmp / "out.txt"
        out.write_text("")
        done = self.run_script(
            "8.", MODE="pr", EVENT="pull_request", SCANNER=str(SCANNER), BASE_SHA=base, HEAD_SHA=head,
            CHANGED_FILES="1", OWN_REPO="owner/public-repo", EXTRA_REFS="", DENY_PARAM="",
            PR_TITLE="A title", PR_BODY="A body", ISSUE_TITLE="", ISSUE_BODY="",
            GITHUB_OUTPUT=str(out), RUNNER_TEMP=str(self.tmp),
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return dict(line.split("=", 1) for line in out.read_text().splitlines())["worst"]

    def test_the_old_head_is_recovered_through_the_pull_ref_and_scans_clean(self):
        base, head = self.build("an ordinary line")
        done = self.fetch(base, head)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.scan(base, head), "0")

    def test_a_denied_line_on_the_recovered_head_still_fails(self):
        # Built at run time so this file's own diff carries no hostname.
        base, head = self.build("see " + "srv" + "-box-01")
        done = self.fetch(base, head)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.scan(base, head), "1")

    def test_an_unrecoverable_head_fails_closed(self):
        base, head = self.build("an ordinary line", keep_pull_ref=False)
        done = self.fetch(base, head)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::", done.stdout)
        self.assertIn(head, done.stdout)

    def test_a_pull_ref_at_another_commit_is_not_taken_for_the_head(self):
        base, _ = self.build("an ordinary line")
        done = self.fetch(base, "0" * 40)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::", done.stdout)

    def test_an_unrecoverable_base_fails_closed(self):
        _, head = self.build("an ordinary line")
        done = self.fetch("1" * 40, head)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::", done.stdout)

    def test_the_fetch_runs_only_for_pull_requests_in_pr_mode(self):
        cond, _ = step(FETCH_STEP)
        self.assertEqual(cond, "github.event_name == 'pull_request' && inputs.mode == 'pr'")


if __name__ == "__main__":
    unittest.main(verbosity=2)
