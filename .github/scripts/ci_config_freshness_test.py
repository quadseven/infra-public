#!/usr/bin/env python3
"""Fixture suite for ci_config_freshness.py and the compare step of
_reusable.ci-config-freshness.yml (infra-public#168).

Each case builds a small throwaway git repository - a base branch and a PR
branch - and checks the verdict: stale (exit 1, commits named), current
(exit 0) or cannot check (exit 2). The cannot-check cases matter most: a
freshness check that passes on a shallow clone or an unknown ref certifies
exactly the stale branch it exists to stop.

The last class lifts step 5's `run:` script out of the workflow file as it is
on disk and runs it under bash against a fixture repository, so the
workflow's wiring (which refs, which flags, the default path list) is tested,
not only the script.

Python stdlib only (unittest), plus git and bash.

Run: python3 .github/scripts/ci_config_freshness_test.py
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ci_config_freshness as ccf

HERE = Path(__file__).resolve().parent
WORKFLOW = HERE.parent / "workflows" / "_reusable.ci-config-freshness.yml"
STEP_RE = re.compile(r"^(\s*)- name:\s*(.+?)\s*$")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


class Repo:
    """A scratch repository with helpers to commit files on branches."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.commit(
            "initial",
            {"README.md": "hello\n", ".github/workflows/ci.yml": "name: ci\n"},
        )

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ENV},
            check=True,
        )
        return done.stdout.strip()

    def commit(self, subject: str, files: dict[str, str]) -> str:
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str, start: str = "main") -> None:
        self.git("switch", "-q", "-c", name, start)

    def switch(self, name: str) -> None:
        self.git("switch", "-q", name)


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ccf.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="ci-freshness-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.repo = Repo(self.tmp / "repo")
        self.repo.branch("pr")
        self.repo.commit("feature work", {"src/app.py": "print(1)\n"})
        self.repo.switch("main")

    def verdict(self, *extra: str) -> tuple[int, str, str]:
        return run_main(
            "--repo", str(self.repo.root), "--base", "main", "--head", "pr", *extra
        )


class Stale(Fixture):
    def test_a_workflow_commit_on_the_base_fails_and_is_named(self):
        sha = self.repo.commit(
            "ci: add a lint gate", {".github/workflows/lint.yml": "name: lint\n"}
        )
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.STALE, out)
        self.assertIn(sha[:12], out)
        self.assertIn("ci: add a lint gate", out)
        self.assertIn(".github/workflows/lint.yml", out)
        self.assertIn("git rebase main", out)
        self.assertIn("git merge main", out)

    def test_every_default_path_counts(self):
        for name in (
            ".github/actions/setup/action.yml",
            ".github/rulesets/main.json",
            ".loom/validation.conf",
        ):
            with self.subTest(name=name):
                self.repo.commit(f"change {name}", {name: "x\n"})
                code, out, _ = self.verdict()
                self.assertEqual(code, ccf.STALE, out)
                self.assertIn(name, out)

    def test_only_watched_commits_and_files_are_listed(self):
        self.repo.commit("docs only", {"docs/guide.md": "words\n"})
        self.repo.commit(
            "ci and docs together",
            {".github/workflows/ci.yml": "name: ci2\n", "docs/other.md": "more\n"},
        )
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.STALE, out)
        self.assertIn("1 commit changing CI config", out)
        self.assertNotIn("docs only", out)
        self.assertNotIn("docs/other.md", out)

    def test_a_merge_commit_on_the_base_is_named(self):
        self.repo.branch("side")
        self.repo.commit(
            "side: tighten ci", {".github/workflows/ci.yml": "name: strict\n"}
        )
        self.repo.switch("main")
        self.repo.commit("unrelated", {"notes.txt": "n\n"})
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge side into main", "side")
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.STALE, out)
        self.assertIn("Merge side into main", out)
        self.assertIn(".github/workflows/ci.yml", out)

    def test_custom_paths_replace_the_defaults(self):
        self.repo.commit("ci: workflow", {".github/workflows/x.yml": "x\n"})
        code, out, _ = self.verdict("--paths", "ci/, Makefile")
        self.assertEqual(code, ccf.OK, out)
        self.repo.commit("ci: script", {"ci/run.sh": "true\n"})
        code, out, _ = self.verdict("--paths", "ci/\nMakefile  # build entry")
        self.assertEqual(code, ccf.STALE, out)
        self.assertIn("ci/run.sh", out)
        self.assertNotIn(".github/workflows/x.yml", out)


class Current(Fixture):
    def test_base_moved_only_unwatched_paths(self):
        self.repo.commit("docs", {"docs/guide.md": "words\n"})
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.OK, out)
        self.assertIn("OK:", out)

    def test_base_did_not_move(self):
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.OK, out)

    def test_pr_that_merged_the_base_in(self):
        self.repo.commit("ci: new gate", {".github/workflows/gate.yml": "g\n"})
        self.repo.switch("pr")
        self.repo.git("merge", "-q", "--no-edit", "main")
        self.repo.switch("main")
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.OK, out)

    def test_pr_rebased_onto_the_base(self):
        self.repo.commit("ci: new gate", {".github/workflows/gate.yml": "g\n"})
        self.repo.switch("pr")
        self.repo.git("rebase", "-q", "main")
        self.repo.switch("main")
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.OK, out)

    def test_ci_change_on_the_pr_branch_itself_is_not_stale(self):
        self.repo.switch("pr")
        self.repo.commit("ci: my own change", {".github/workflows/ci.yml": "mine\n"})
        self.repo.switch("main")
        code, out, _ = self.verdict()
        self.assertEqual(code, ccf.OK, out)


class CannotCheck(Fixture):
    def test_unknown_base_ref(self):
        code, _, err = run_main(
            "--repo", str(self.repo.root), "--base", "origin/nope", "--head", "pr"
        )
        self.assertEqual(code, ccf.CANNOT_CHECK)
        self.assertIn("does not resolve", err)

    def test_unknown_head_ref(self):
        code, _, err = run_main(
            "--repo", str(self.repo.root), "--base", "main", "--head", "0" * 40
        )
        self.assertEqual(code, ccf.CANNOT_CHECK)
        self.assertIn("does not resolve", err)

    def test_shallow_clone(self):
        self.repo.commit("ci: gate", {".github/workflows/gate.yml": "g\n"})
        shallow = self.tmp / "shallow"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                "--no-single-branch",
                f"file://{self.repo.root}",
                str(shallow),
            ],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )
        code, _, err = run_main(
            "--repo", str(shallow), "--base", "origin/main", "--head", "origin/pr"
        )
        self.assertEqual(code, ccf.CANNOT_CHECK)
        self.assertIn("shallow", err)

    def test_no_common_history(self):
        self.repo.git("switch", "-q", "--orphan", "island")
        self.repo.commit("island root", {"x.txt": "x\n"})
        code, _, err = run_main(
            "--repo", str(self.repo.root), "--base", "main", "--head", "island"
        )
        self.assertEqual(code, ccf.CANNOT_CHECK)
        self.assertIn("share no history", err)

    def test_not_a_repository(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, _, _ = run_main("--repo", str(empty), "--base", "main")
        self.assertEqual(code, ccf.CANNOT_CHECK)

    def test_empty_or_option_like_path_list(self):
        for paths in ("", " , ,", "# only a comment", "--all"):
            with self.subTest(paths=paths):
                code, _, _ = self.verdict(f"--paths={paths}")
                self.assertEqual(code, ccf.CANNOT_CHECK)

    def test_github_mode_annotates_a_failure_to_run(self):
        code, out, _ = run_main(
            "--repo", str(self.repo.root), "--base", "nope", "--github"
        )
        self.assertEqual(code, ccf.CANNOT_CHECK)
        self.assertTrue(
            out.startswith("::error title=CI config freshness could not run::"), out
        )


class GithubOutput(Fixture):
    def test_annotation_and_summary_for_a_stale_branch(self):
        sha = self.repo.commit(
            "ci: 100% stricter\r\n::warning::x", {".github/workflows/x.yml": "x\n"}
        )
        summary = self.tmp / "summary.md"
        old = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        self.addCleanup(
            lambda: (
                os.environ.pop("GITHUB_STEP_SUMMARY")
                if old is None
                else os.environ.update(GITHUB_STEP_SUMMARY=old)
            )
        )
        code, out, _ = self.verdict("--github")
        self.assertEqual(code, ccf.STALE, out)
        notes = [line for line in out.splitlines() if line.startswith("::")]
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("::error title=CI config is stale::"))
        self.assertIn(sha[:12], notes[0])
        self.assertIn("100%25 stricter", notes[0])
        self.assertIn("Rebase onto main or merge it in", notes[0])
        text = summary.read_text()
        self.assertIn("### CI config is stale", text)
        self.assertIn(f"`{sha[:12]}`", text)

    def test_escape_annotation(self):
        self.assertEqual(ccf.escape_annotation("a%b\r\nc"), "a%25b%0D%0Ac")


def step_script(prefix: str) -> str:
    """The dedented `run: |` block of the workflow step whose name starts with
    `prefix`, as the file on disk has it."""
    lines = WORKFLOW.read_text().splitlines()
    for i, line in enumerate(lines):
        m = STEP_RE.match(line)
        if not m or not m.group(2).strip("\"'").startswith(prefix):
            continue
        step_indent = len(m.group(1))
        for j in range(i + 1, len(lines)):
            cur = lines[j]
            if cur.strip() and len(cur) - len(cur.lstrip()) <= step_indent:
                break
            if re.match(r"^\s*run:\s*\|\s*$", cur):
                key_indent = len(cur) - len(cur.lstrip())
                body = []
                for k in range(j + 1, len(lines)):
                    nxt = lines[k]
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= key_indent:
                        break
                    body.append(nxt)
                indent = min(len(b) - len(b.lstrip()) for b in body if b.strip())
                return "\n".join(b[indent:] for b in body) + "\n"
        raise AssertionError(f"step {prefix!r} has no `run: |` block")
    raise AssertionError(f"no step starting {prefix!r} in {WORKFLOW.name}")


class Workflow(unittest.TestCase):
    """The reusable as written: the checkout it does and the step it runs."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="ci-freshness-wf-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        # The layout the workflow builds: the caller at _repo with an origin
        # remote (as actions/checkout leaves it), the script at _tool.
        upstream = Repo(self.tmp / "upstream")
        upstream.branch("pr")
        self.head = upstream.commit("feature", {"src/a.py": "a\n"})
        upstream.switch("main")
        self.upstream = upstream
        tool = self.tmp / "ws" / "_tool" / ".github" / "scripts"
        tool.mkdir(parents=True)
        shutil.copy(HERE / "ci_config_freshness.py", tool / "ci_config_freshness.py")

    def clone(self) -> None:
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                f"file://{self.upstream.root}",
                str(self.tmp / "ws" / "_repo"),
            ],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )

    def run_step(
        self, paths: str = "", base: str = "main"
    ) -> subprocess.CompletedProcess[str]:
        ws = self.tmp / "ws"
        return subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", step_script("5.")],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                **GIT_ENV,
                "TOOL": str(
                    ws / "_tool" / ".github" / "scripts" / "ci_config_freshness.py"
                ),
                "REPO_DIR": str(ws / "_repo"),
                "BASE": base,
                "HEAD_SHA": self.head,
                "PATHS": paths,
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_checkout_is_the_pr_head_with_full_history(self):
        text = WORKFLOW.read_text()
        block = text.split('- name: "3.', 1)[1].split("- name:", 1)[0]
        self.assertIn("ref: ${{ github.event.pull_request.head.sha }}", block)
        self.assertIn("fetch-depth: 0", block)
        self.assertIn("path: _repo", block)

    def test_step_fails_on_a_stale_head(self):
        sha = self.upstream.commit(
            "ci: new gate", {".github/workflows/gate.yml": "g\n"}
        )
        self.clone()
        done = self.run_step()
        self.assertEqual(done.returncode, ccf.STALE, done.stdout + done.stderr)
        self.assertIn(sha[:12], done.stdout)
        self.assertIn("::error title=CI config is stale::", done.stdout)
        self.assertIn("origin/main", done.stdout)

    def test_step_passes_on_a_current_head(self):
        self.upstream.commit("docs", {"docs/a.md": "a\n"})
        self.clone()
        done = self.run_step()
        self.assertEqual(done.returncode, ccf.OK, done.stdout + done.stderr)

    def test_step_passes_the_caller_path_list(self):
        self.upstream.commit("ci: script", {"ci/run.sh": "true\n"})
        self.clone()
        self.assertEqual(self.run_step().returncode, ccf.OK)
        done = self.run_step(paths="ci/")
        self.assertEqual(done.returncode, ccf.STALE, done.stdout + done.stderr)

    def test_step_fails_closed_without_the_script(self):
        self.clone()
        (
            self.tmp / "ws" / "_tool" / ".github" / "scripts" / "ci_config_freshness.py"
        ).unlink()
        done = self.run_step()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("was not fetched", done.stdout)

    def test_step_fails_closed_on_an_unknown_base(self):
        self.clone()
        done = self.run_step(base="does-not-exist")
        self.assertEqual(done.returncode, ccf.CANNOT_CHECK, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
