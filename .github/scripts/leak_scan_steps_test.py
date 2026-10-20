#!/usr/bin/env python3
"""Runs the issue-comment step of _reusable.leak-scan.yml under bash.

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
  cat "$GH_COMMENTS"
fi
"""

BOT = "github-actions[bot]"
FINDING_OUTPUT = """BLOCKED: private infrastructure identifiers found (9 shape patterns)

  {path}:1  [private-issue-ref] 'other-repo#N' - a cross-repo issue reference (may name a private repo)
  {path}:4  [deny-list] <redacted, 6 chars> - a deny-listed term

This repo is PUBLIC.
"""


@unittest.skipUnless(shutil.which("jq"), "jq is on the runner; install it to run this here")
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

    def run_step(self, worst: str, comments: list[dict], scan_output: str = "") -> list[list[str]]:
        self.comments.write_text(json.dumps(comments))
        self.scan.write_text(scan_output.format(path=self.tmp / "text.md"))
        _, script = step("9.")
        done = subprocess.run(
            ["bash", "-e", "-c", script],
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
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        calls = [c.split("\0")[:-1] for c in self.log.read_text().split("\036") if c]
        return [c for c in calls if not (c[0] == "api" and "-X" not in c)]

    def body_of(self, call: list[str]) -> str:
        for i, arg in enumerate(call):
            if arg == "--body":
                return call[i + 1]
            if arg.startswith("body="):
                return arg[len("body="):]
        raise AssertionError(f"no body in {call}")

    def test_the_comment_names_each_flagged_line(self):
        writes = self.run_step("1", [], FINDING_OUTPUT)
        self.assertEqual(len(writes), 1, writes)
        body = self.body_of(writes[0])
        self.assertIn("title: [private-issue-ref] 'other-repo#N'", body)
        self.assertIn("body line 3: [deny-list] <redacted, 6 chars>", body)

    def test_a_flagged_line_is_quoted_in_a_code_block_so_nothing_links(self):
        body = self.body_of(self.run_step("1", [], FINDING_OUTPUT)[0])
        block = body.split("```")
        self.assertGreaterEqual(len(block), 3, body)
        self.assertIn("other-repo#N", block[1])
        self.assertNotIn("other-repo#N", block[0] + block[2])

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
        cond, _ = step("9.")
        self.assertIn("github.event_name == 'issues'", cond)
        self.assertIn("steps.scan.outputs.worst == '0'", cond)
        self.assertIn("steps.scan.outputs.worst == '1'", cond)


if __name__ == "__main__":
    unittest.main()
