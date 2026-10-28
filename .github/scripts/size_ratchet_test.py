#!/usr/bin/env python3
"""Real Git/CLI regressions and execution of the reusable writer's shell step."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from deploy_k8s_steps_test import run, step_script

HERE = Path(__file__).resolve().parent
TOOL = HERE / "size_ratchet.py"
WORKFLOW = HERE.parent / "workflows" / "_reusable.size-ratchet.yml"
RECORDS = ".size-ratchet-records.json"
POLICY = ".size-ratchet.json"


class Ratchet(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "_repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.com")
        self.write(POLICY, {"default": 3, "overrides": {"tests/": 5}})
        self.write(RECORDS, {"large.py": 8})
        self.lines("large.py", 8)
        self.lines("small.py", 3)
        self.base = self.save()

    def git(self, *args):
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True, text=True,
            check=True, timeout=30,
        )
        return result.stdout.strip()

    def write(self, path, value):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def lines(self, path, count, final_lf=True):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x\n" * max(0, count - 1) + (b"x\n" if final_lf else b"x"))

    def save(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, mode="check", base=None, **kwargs):
        args = [sys.executable, str(TOOL), mode, "--repo", str(self.repo),
                "--base", base or self.base]
        for name, value in kwargs.items():
            args += [f"--{name}", value]
        return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)

    def assert_code(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_unchanged_and_shrinking_files_pass(self):
        self.assert_code(self.check(), 0)
        self.lines("large.py", 6)
        self.save()
        self.assert_code(self.check(), 0)

    def test_recorded_growth_fails_even_if_pr_raises_policy(self):
        self.lines("large.py", 9)
        self.write(POLICY, {"default": 1000})
        self.save()
        result = self.check()
        self.assert_code(result, 1)
        self.assertIn("record 8", result.stdout)

    def test_new_over_ceiling_file_fails_even_if_pr_raises_policy(self):
        self.lines("new.py", 4)
        self.write(POLICY, {"default": 1000})
        self.save()
        self.assert_code(self.check(), 1)

    def test_record_edit_alone_fails(self):
        self.write(RECORDS, {"large.py": 99})
        self.save()
        self.assert_code(self.check(), 1)

    def test_adding_exception_for_new_file_fails(self):
        self.lines("new.py", 10)
        self.write(RECORDS, {"large.py": 8, "new.py": 10})
        self.save()
        result = self.check()
        self.assert_code(result, 1)
        self.assertIn("new.py: 10 lines exceeds ceiling 3", result.stdout)

    def test_deleting_records_fails(self):
        (self.repo / RECORDS).unlink()
        self.save()
        self.assert_code(self.check(), 1)

    def test_longest_folder_override_and_final_unterminated_line(self):
        self.write(POLICY, {"default": 3, "overrides": {"tests/": 5, "tests/data/": 6}})
        self.base = self.save()
        self.lines("tests/data/sample.txt", 6, final_lf=False)
        self.lines("tests/example.py", 5)
        self.save()
        self.assert_code(self.check(), 0)
        self.lines("tests/data/sample.txt", 7, final_lf=False)
        self.save()
        self.assert_code(self.check(), 1)

    def test_invalid_base_fails_closed(self):
        self.assert_code(self.check(base="missing-ref"), 2)

    def test_malformed_trusted_policy_fails_closed(self):
        self.write(POLICY, {"default": True})
        self.base = self.save()
        self.assert_code(self.check(), 2)

    def test_invalid_record_paths_fail_closed(self):
        self.write(RECORDS, {"../outside": 20})
        self.base = self.save()
        self.assert_code(self.check(), 2)

    def test_lower_only_shrinks_and_removes_records(self):
        self.lines("large.py", 6)
        self.save()
        self.assert_code(self.check("lower"), 0)
        self.assertEqual(json.loads((self.repo / RECORDS).read_text()), {"large.py": 6})
        self.base = self.save()
        self.lines("large.py", 3)
        self.save()
        self.assert_code(self.check("lower"), 0)
        self.assertEqual(json.loads((self.repo / RECORDS).read_text()), {})

    def test_lower_removes_deleted_file(self):
        (self.repo / "large.py").unlink()
        self.save()
        self.assert_code(self.check("lower"), 0)
        self.assertEqual(json.loads((self.repo / RECORDS).read_text()), {})

    def test_lower_never_raises_records(self):
        self.lines("large.py", 9)
        self.save()
        self.assert_code(self.check("lower"), 1)
        self.assertEqual(json.loads((self.repo / RECORDS).read_text()), {"large.py": 8})

    def test_lower_rejects_dirty_tree_and_wrong_head(self):
        self.lines("small.py", 2)
        self.assert_code(self.check("lower"), 2)
        self.save()
        self.assert_code(self.check("lower", head=self.base), 2)

    def test_binary_blob_lf_lines_are_not_skipped(self):
        (self.repo / "new.bin").write_bytes(b"\0\n" * 4)
        self.save()
        self.assert_code(self.check(), 1)

    def test_non_ascii_and_space_filename_is_counted(self):
        self.lines("a space \u00e9.py", 4)
        self.save()
        self.assert_code(self.check(), 1)

    def test_actual_writer_step_commits_and_pushes_lower_record(self):
        remote = self.root / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.lines("large.py", 6)
        head = self.save()
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        tool_dir = self.root / "_tool" / ".github" / "scripts"
        tool_dir.mkdir(parents=True)
        shutil.copyfile(TOOL, tool_dir / TOOL.name)
        env = {"BASE": self.base, "HEAD": head, "POLICY": POLICY,
               "RECORDS": RECORDS, "BRANCH": "main", "GH_TOKEN": "fixture-only"}
        done = run(step_script("Lower and commit records", WORKFLOW.read_text()), env, str(self.repo))
        self.assert_code(done, 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("log", "-1", "--format=%an"), "github-actions[bot]")
        self.assertEqual(json.loads(self.git("show", f"HEAD:{RECORDS}")), {"large.py": 6})
        observed = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
                                  capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(observed.stdout.strip(), self.git("rev-parse", "HEAD"))
        env.update(BASE=head, HEAD=self.git("rev-parse", "HEAD"))
        # A bot record commit is refused as an input event, never raises records.
        self.assert_code(run(step_script("Lower and commit records", WORKFLOW.read_text()), env, str(self.repo)), 1)

    def test_writer_normal_push_refuses_concurrent_remote_update(self):
        remote = self.root / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.lines("large.py", 6)
        head = self.save()
        self.lines("small.py", 2)
        newer = self.save()
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.git("checkout", "--detach", head)
        tool_dir = self.root / "_tool" / ".github" / "scripts"
        tool_dir.mkdir(parents=True)
        shutil.copyfile(TOOL, tool_dir / TOOL.name)
        done = run(step_script("Lower and commit records", WORKFLOW.read_text()),
                   {"BASE": self.base, "HEAD": head, "POLICY": POLICY,
                    "RECORDS": RECORDS, "BRANCH": "main", "GH_TOKEN": "fixture-only"}, str(self.repo))
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        observed = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
                                  capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(observed.stdout.strip(), newer)

    def test_writer_without_record_file_makes_no_commit(self):
        (self.repo / RECORDS).unlink()
        self.lines("large.py", 3)
        self.base = self.save()
        tool_dir = self.root / "_tool" / ".github" / "scripts"
        tool_dir.mkdir(parents=True)
        shutil.copyfile(TOOL, tool_dir / TOOL.name)
        done = run(step_script("Lower and commit records", WORKFLOW.read_text()),
                   {"BASE": self.base, "HEAD": self.base, "POLICY": POLICY,
                    "RECORDS": RECORDS, "BRANCH": "main", "GH_TOKEN": "fixture-only"}, str(self.repo))
        self.assert_code(done, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertFalse((self.repo / RECORDS).exists())


class Workflow(unittest.TestCase):
    def test_writer_is_default_branch_push_only_and_check_is_read_only(self):
        text = WORKFLOW.read_text()
        check, lower = text.split("\n  lower:\n")
        check = check.split("\njobs:\n", 1)[1]
        self.assertIn("if: github.event_name == 'pull_request'", check)
        self.assertIn("contents: read", check)
        self.assertNotIn("contents: write", check)
        self.assertIn("github.event_name == 'push'", lower)
        self.assertIn("github.event.repository.default_branch", lower)
        self.assertIn("!github.event.deleted", lower)
        self.assertIn("BASE: ${{ github.event.before }}", lower)
        self.assertNotIn("pull_request_target", text)
        self.assertEqual(text.count("persist-credentials: false"), 4)
        self.assertEqual(text.count("repository: quadseven/infra-public"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
