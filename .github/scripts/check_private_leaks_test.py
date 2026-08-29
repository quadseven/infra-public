#!/usr/bin/env python3
"""Fixture suite for check_private_leaks.py.

Two things are pinned here, and the second matters more than the first.

1. MATCHING. Every generic shape has a positive and a negative fixture, so a
   regex tweak cannot quietly stop matching a class of leak, and the
   false-positive exemptions that make the guard survivable (a CI runner's
   /home/runner path, this repo's own bare issue refs, a scrub's `-` line) are
   held in place.

2. FAILING CLOSED. The reason this repo has a leak guard at all is that a check
   which degrades to green is worse than no check - it certifies. So the exit-2
   paths are exercised through the REAL CLI against a REAL git repo: an
   unresolvable diff range, an empty diff under --require-changes, an SSM read
   that errors, and an SSM parameter that resolves to nothing. Each must exit
   non-zero. A unit test that only proved the happy path would leave exactly the
   failure this whole thing exists to prevent untested.

Lines carrying a deliberate fixture leak are marked `leak-guard-allow` so the
guard can scan its own test suite without blocking on its own fixtures - the
marker is noisy on purpose, so an exemption is visible in review.

Python stdlib only (unittest), matching the script's zero-dependency design.

Run: python3 .github/scripts/check_private_leaks_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from check_private_leaks import (
    build_patterns,
    load_ssm_deny_list,
    repo_ref_pattern,
    scan,
)

SCRIPT = Path(__file__).with_name("check_private_leaks.py")
RULES = build_patterns(["infra-public"])


def hits(text: str, *, diff_mode: bool = False, rules=None) -> list[str]:
    return scan(text, rules or RULES, label="t", diff_mode=diff_mode)


class GenericShapes(unittest.TestCase):
    """One positive per shape. A leak class with no fixture is a leak class
    that can be deleted by accident."""

    def test_tailnet_host(self):
        self.assertIn("tailnet-host", hits("box.ts.example.net")[0])  # leak-guard-allow: fixture

    def test_tailscale_cgnat_ip(self):
        self.assertIn("tailscale-ip", hits("100.101.102.103")[0])  # leak-guard-allow: fixture

    def test_rfc1918_ip(self):
        for ip in ("10.1.2.3", "192.168.4.5", "172.20.0.9"):  # leak-guard-allow: fixture
            with self.subTest(ip=ip):
                self.assertIn("rfc1918-ip", hits(ip)[0])

    def test_public_ip_is_clean(self):
        # 203.0.113.0/24 is RFC 5737 documentation space - what AGENTS.md tells
        # people to use instead. It must never trip the guard.
        self.assertEqual(hits("203.0.113.7"), [])

    def test_server_hostname(self):
        self.assertIn("server-hostname", hits("srv-thing-01")[0])  # leak-guard-allow: fixture

    def test_cluster_node(self):
        self.assertIn("cluster-node", hits("k8s-abc-pool-worker-2")[0])  # leak-guard-allow: fixture

    def test_mac_address(self):
        self.assertIn("mac-address", hits("a1:b2:c3:d4:e5:f6")[0])  # leak-guard-allow: fixture

    def test_version_string_is_not_a_mac(self):
        self.assertEqual(hits("timings 01:02:03 and 1.2.3"), [])


class LocalUserPaths(unittest.TestCase):
    """AGENTS.md forbids a path that reveals a real local username. The runner
    and container accounts must stay clean or every Actions log line is a hit."""

    def test_macos_home_is_caught(self):
        self.assertIn("local-user-path", hits("/Users/somebody/dev/x")[0])  # leak-guard-allow: fixture

    def test_linux_home_is_caught(self):
        self.assertIn("local-user-path", hits("/home/somebody/dev/x")[0])  # leak-guard-allow: fixture

    def test_windows_home_is_caught(self):
        self.assertIn("local-user-path", hits(r"C:\Users\somebody\dev")[0])  # leak-guard-allow: fixture

    def test_ci_runner_home_is_clean(self):
        self.assertEqual(hits("/home/runner/work/repo/repo"), [])

    def test_container_and_placeholder_accounts_are_clean(self):
        for p in ("/home/root/x", "/home/node/app", "/Users/you/dev", "/home/appuser/x"):
            with self.subTest(path=p):
                self.assertEqual(hits(p), [])


class CrossRepoRefs(unittest.TestCase):
    """The rule that is parameterized per consuming repo. Measured against this
    repo's full history before shipping: the only hits were its OWN name and
    deliberate refs to the private tracker."""

    def test_own_repo_bare_ref_is_clean(self):
        self.assertEqual(hits("fixed in infra-public#46"), [])

    def test_own_repo_qualified_ref_is_clean(self):
        self.assertEqual(hits("fixed in quadseven/infra-public#46"), [])

    def test_other_repo_bare_ref_is_caught(self):
        self.assertIn("private-issue-ref", hits("see someplace#12")[0])  # leak-guard-allow: fixture

    def test_other_repo_qualified_ref_is_caught(self):
        # grug's original missed this form entirely: the lookbehind that stops
        # the repo half of a qualified ref matching on its own also
        # let the whole qualified ref sail through - the form that names
        # the owner too.
        self.assertIn("private-issue-ref", hits("see someone/someplace#12")[0])  # leak-guard-allow: fixture

    def test_bare_issue_number_is_clean(self):
        self.assertEqual(hits("closes #123"), [])

    def test_hyphenated_word_before_own_issue_is_clean(self):
        # "Post-#77" is a word followed by this repo's issue, not a repo ref.
        self.assertEqual(hits("Post-#77 cleanup and pre-#354 review"), [])

    def test_issue_url_is_clean(self):
        self.assertEqual(hits("https://github.com/quadseven/infra-public/issues/1"), [])

    def test_no_allow_list_still_builds_a_valid_pattern(self):
        # An empty allow-list must not produce an empty or always-matching
        # regex; it just means nothing is exempt.
        rules = build_patterns([])
        self.assertIn("private-issue-ref", hits("infra-public#46", rules=rules)[0])
        self.assertEqual(hits("closes #7", rules=rules), [])

    def test_allow_list_is_regex_escaped(self):
        # A repo name is spliced into a regex; a dot in it must stay literal.
        pattern = repo_ref_pattern(["my.repo"])
        self.assertIn(r"my\.repo", pattern)


class DiffSemantics(unittest.TestCase):
    def test_removed_line_is_never_blocked(self):
        # A `-` line is someone SCRUBBING a leak. Blocking it would make the
        # guard forbid its own remedy.
        self.assertEqual(hits("-old 10.1.2.3 here", diff_mode=True), [])  # leak-guard-allow: fixture

    def test_added_line_is_blocked(self):
        self.assertIn("rfc1918-ip", hits("+new 10.1.2.3 here", diff_mode=True)[0])  # leak-guard-allow: fixture

    def test_diff_file_headers_are_skipped(self):
        text = "--- a/10.1.2.3.txt\n+++ b/10.1.2.3.txt\n"  # leak-guard-allow: fixture
        self.assertEqual(hits(text, diff_mode=True), [])

    def test_markdown_bullet_is_scanned_in_text_mode(self):
        # The same leading `-` that means "removal" in a diff means "bullet" in
        # a PR body, and most of the leaks this guard exists for were prose.
        self.assertIn("rfc1918-ip", hits("- the host at 10.1.2.3", diff_mode=False)[0])  # leak-guard-allow: fixture

    def test_allow_marker_exempts_a_line(self):
        self.assertEqual(hits("10.1.2.3  # leak-guard-allow: documented example"), [])


class SsmDenyList(unittest.TestCase):
    """Layer 2. Every failure here is fatal by design: a deny-list that does not
    arrive is not 'no extra terms', it is a guard running with the half that
    matches PEOPLE switched off - which is how a personal name sat in a guarded
    repo through two audits that both certified it clean."""

    def _run(self, returncode: int, stdout: str, stderr: str = ""):
        completed = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch("check_private_leaks.subprocess.run", return_value=completed):
            return load_ssm_deny_list("/some/param")

    def test_terms_become_rules(self):
        rules = self._run(0, "alpha beta\n")
        self.assertEqual(len(rules), 2)
        self.assertIn("deny-list", scan("the alpha thing", rules, label="t")[0])

    def test_aws_failure_is_fatal(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(255, "", "AccessDenied")
        self.assertEqual(cm.exception.code, 2)

    def test_empty_parameter_is_fatal(self):
        # A wiped or typo'd parameter must not read as "nothing to deny".
        with self.assertRaises(SystemExit) as cm:
            self._run(0, "\n")
        self.assertEqual(cm.exception.code, 2)


class CliAgainstRealGit(unittest.TestCase):
    """End-to-end through the real CLI and a real git repo. The exit codes are a
    contract the reusable workflow depends on (0 clean / 1 findings / 2 could
    not scan), so they are asserted here rather than assumed."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name)
        cls._git("init", "-q", "-b", "main")
        (cls.repo / "clean.md").write_text("nothing to see here\n")
        cls._git("add", "-A")
        cls._git("commit", "-q", "-m", "base")
        cls.base = cls._git("rev-parse", "HEAD").strip()
        # A seeded leak, exactly as a careless PR would introduce it.
        (cls.repo / "notes.md").write_text("the box at 10.9.8.7 needs a restart\n")  # leak-guard-allow: fixture
        cls._git("add", "-A")
        cls._git("commit", "-q", "-m", "seeded")
        cls.head = cls._git("rev-parse", "HEAD").strip()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def _git(cls, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        return subprocess.run(["git", "-C", str(cls.repo), *args], check=True,
                              capture_output=True, text=True, env=env).stdout

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              cwd=self.repo, capture_output=True, text=True)

    def test_seeded_leak_exits_1(self):
        out = self._cli("--diff", f"{self.base}...{self.head}")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("rfc1918-ip", out.stderr)

    def test_clean_range_exits_0(self):
        out = self._cli("--diff", f"{self.base}...{self.base}")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("clean", out.stdout)

    def test_unresolvable_range_exits_2_not_0(self):
        # THE failure mode this guard exists to avoid: `git diff` on a range
        # whose refs were never fetched returns nothing, and a scanner that
        # ignores the exit code reports "clean" on a diff it never read.
        out = self._cli("--diff", "deadbeef...cafed00d")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("FATAL", out.stderr)

    def test_empty_diff_with_require_changes_exits_2(self):
        out = self._cli("--diff", f"{self.base}...{self.base}", "--require-changes")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("empty", out.stderr)

    def test_empty_diff_without_require_changes_is_clean(self):
        out = self._cli("--diff", f"{self.base}...{self.base}")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_missing_text_file_exits_2(self):
        out = self._cli("--text-file", str(self.repo / "does-not-exist.md"))
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)

    def test_text_file_with_leak_exits_1(self):
        body = self.repo / "body.md"
        body.write_text("- deploy touched 192.168.7.7 tonight\n")  # leak-guard-allow: fixture
        out = self._cli("--text-file", str(body))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("rfc1918-ip", out.stderr)

    def test_own_repo_ref_needs_the_flag(self):
        body = self.repo / "ref.md"
        body.write_text("closes infra-public#46\n")
        self.assertEqual(self._cli("--text-file", str(body)).returncode, 1)
        self.assertEqual(
            self._cli("--text-file", str(body),
                      "--allow-repo-ref", "infra-public").returncode, 0)

    def test_no_source_argument_is_a_usage_error(self):
        self.assertEqual(self._cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()
