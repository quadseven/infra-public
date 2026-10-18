#!/usr/bin/env python3
"""Runs the real leak-scan steps of sync.public-repo-docs.yml under bash
(infra-public#113).

The docs-sync job is the one recurring PR whose whole diff is content copied
in from another repo, so it scans its own staged diff and PR text before it
opens the PR, and opens nothing on a finding. These tests lift two steps out
of the workflow file as it is on disk and run them, in order, against a real
git repo and the real scanner:

- `Prepare sync commit and PR text`
- `Scan sync diff and PR text`

A test that only read the YAML passed while the scan step called the scanner
with two mutually exclusive flags, which is a usage error on every run. Each
case here checks an exit code from the step as written, both ways: a clean
sync passes and a seeded one is refused.

Python stdlib only (unittest), plus bash and git.

Run: python3 .github/scripts/sync_docs_steps_test.py
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy_k8s_steps_test import run, step_script

HERE = Path(__file__).resolve().parent
WORKFLOW = HERE.parent / "workflows" / "sync.public-repo-docs.yml"
SCANNER = HERE / "check_private_leaks.py"

PREPARE = "Prepare sync commit and PR text"
SCAN = "Scan sync diff and PR text"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


# fmt: off
# Fixture lines carry a trailing `leak-guard-allow` marker, which the scanner
# only honors on the SAME line as the hit, so the formatter stays out of here.
class SyncScan(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self.target = self.work / "target"
        self.target.mkdir()
        git(self.target, "init", "-q", "-b", "main")
        (self.target / "AGENTS.md").write_text("# Agents\n\nOld policy.\n")
        (self.target / "CLAUDE.md").write_text("# Claude\n")
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "base")
        text = WORKFLOW.read_text()
        self.prepare = step_script(PREPARE, text)
        self.scan = step_script(SCAN, text)

    def sync(self, agents: str, scanner: Path = SCANNER) -> subprocess.CompletedProcess[str]:
        """Leave AGENTS.md changed and unstaged, as `Diff and stage update`
        does, then run Prepare and Scan in order, as the job does."""
        (self.target / "AGENTS.md").write_text(agents)
        prepared = run(self.prepare, {"BRANCH": "chore/sync-public-repo-docs"}, cwd=str(self.target))
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        return run(self.scan, {"SCANNER": str(scanner)}, cwd=str(self.target))

    def test_a_clean_sync_passes(self):
        done = self.sync("# Agents\n\nNew policy.\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # Both halves ran: the diff and the PR text are each reported clean.
        self.assertEqual(done.stdout.count("leak guard: clean"), 2, done.stdout)

    def test_a_leak_in_the_synced_diff_is_refused(self):
        done = self.sync("# Agents\n\nSee 10.1.2.3 for details.\n")  # leak-guard-allow: fixture
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("BLOCKED", done.stderr)

    def test_a_leak_in_the_pr_text_is_refused(self):
        # Prepare writes the PR text, so seed it after Prepare and before Scan.
        (self.target / "AGENTS.md").write_text("# Agents\n\nNew policy.\n")
        prepared = run(self.prepare, {"BRANCH": "chore/sync-public-repo-docs"}, cwd=str(self.target))
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        with open(self.work / "pr-text.txt", "a", encoding="utf-8") as fh:
            fh.write("Ported from /Users/somebody/dev/x\n")  # leak-guard-allow: fixture
        done = run(self.scan, {"SCANNER": str(SCANNER)}, cwd=str(self.target))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("BLOCKED", done.stderr)

    def test_the_scan_covers_only_the_sync_commit(self):
        # A leak already on the default branch is not this PR's diff; the
        # full-tree sweep owns that. Only lines the sync adds are scanned.
        (self.target / "CLAUDE.md").write_text("# Claude\n\nhost 10.1.2.3\n")  # leak-guard-allow: fixture
        git(self.target, "commit", "-qam", "pre-existing")
        done = self.sync("# Agents\n\nNew policy.\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_missing_scanner_fails_closed(self):
        done = self.sync("# Agents\n\nNew policy.\n", scanner=self.work / "absent.py")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("leak scanner was not fetched", done.stdout)
# fmt: on


if __name__ == "__main__":
    unittest.main(verbosity=2)
