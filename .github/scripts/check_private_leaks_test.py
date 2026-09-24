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
    deny_rule,
    load_ssm_deny_list,
    parse_deny_list,
    repo_ref_pattern,
    scan,
)

SCRIPT = Path(__file__).with_name("check_private_leaks.py")
RULES = build_patterns(["infra-public"])


def hits(text: str, *, diff_mode: bool = False, rules=None) -> list[str]:
    return scan(text, rules or RULES, label="t", diff_mode=diff_mode)


# fmt: off
# Below here, fixture lines carry a trailing `leak-guard-allow` marker, which
# the scanner only honors on the SAME line as the hit. `ruff format` wraps
# long calls and moves a trailing comment to the closing bracket, orphaning
# the fixture from its marker, so the formatter stays out of the tests.
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

    def test_longer_dotted_number_run_is_not_an_ip(self):
        # An SVG path or a version string holds runs of dotted numbers; four
        # octets taken from the middle of a longer run are not an address.
        # Found by the first full-tree sweep (an inline SVG logo).
        self.assertEqual(hits('d="M7.86 10.92.58.11.79-.25"'), [])
        self.assertEqual(hits("v1.10.2.3.4 and 1.100.64.1.1"), [])

    def test_ip_at_end_of_sentence_is_still_caught(self):
        self.assertIn("rfc1918-ip", hits("the host was 10.1.2.3.")[0])  # leak-guard-allow: fixture


class StrayMention(unittest.TestCase):
    """A mention notifies and subscribes a real user; it cannot be undone."""

    def test_bare_mention_is_caught(self):
        self.assertIn("stray-mention", hits("@grug re-review")[0])  # leak-guard-allow: fixture

    def test_mention_mid_sentence_is_caught(self):
        self.assertIn("stray-mention", hits("thanks @grug, looks good")[0])  # leak-guard-allow: fixture

    def test_backticked_handle_is_clean(self):
        self.assertEqual(hits("never write `@grug` on GitHub"), [])

    def test_slash_command_and_bot_name_are_clean(self):
        self.assertEqual(hits("/grug improve, or ask grug-tribe[bot]"), [])

    def test_email_and_decorator_are_clean(self):
        for text in ("mail grug@example.com", "@grug_command", "@grug.register"):
            with self.subTest(text=text):
                self.assertEqual(hits(text), [])


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

    def test_url_path_is_not_a_home_directory(self):
        # github.com/users/<org>/projects is a web page. Found by the first
        # full-tree sweep.
        self.assertEqual(hits("https://github.com/users/someorg/projects/1"), [])

    def test_home_path_after_a_quote_or_colon_is_caught(self):
        for text in ('"/Users/somebody/x"', "PATH=/bin:/home/somebody/bin"):  # leak-guard-allow: fixture
            with self.subTest(text=text):
                self.assertIn("local-user-path", hits(text)[0])

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

    def test_all_caps_key_prefix_is_not_a_repo(self):
        # Sort-key prefixes (`USER#1`, `INST#42`) and "PR#734" are not repo
        # refs. Found by the first full-tree sweep: 30+ test fixtures.
        for text in ("pk USER#100", "sk INST#42", "see #730/PR#734", "REVIEW#7"):
            with self.subTest(text=text):
                self.assertEqual(hits(text), [])

    def test_lower_and_mixed_case_repo_refs_are_still_caught(self):
        for text in ("see someplace#12", "see SomePlace#12", "see some-place#12"):  # leak-guard-allow: fixture
            with self.subTest(text=text):
                self.assertIn("private-issue-ref", hits(text)[0])

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
        rules = self._run(0, "alpha\nbeta\n")
        self.assertEqual(len(rules), 2)
        self.assertIn("deny-list", scan("the alpha thing", rules, label="t")[0])

    def test_one_term_per_line_keeps_a_phrase_whole(self):
        # A full name is ONE term. Whitespace splitting would make each word
        # its own rule, so the second word alone would fire on prose.
        rules = self._run(0, "ada lovelace\n")
        self.assertEqual(len(rules), 1)
        self.assertEqual(scan("lovelace alone", rules, label="t"), [])
        self.assertEqual(len(scan("met Ada Lovelace", rules, label="t")), 1)

    def test_comments_and_blanks_are_not_terms(self):
        # The real deny-list documents itself with `#` lines. Read as terms,
        # every word of a comment would fire on ordinary prose.
        raw = "# operator identity: first name, surname\n\n  alpha  \n# x\n"
        self.assertEqual(parse_deny_list(raw), ["alpha"])

    def test_duplicates_collapse_case_insensitively(self):
        self.assertEqual(parse_deny_list("Alpha\nalpha\nALPHA\n"), ["Alpha"])

    def test_comment_only_parameter_is_fatal(self):
        # Present but with no active term is the same inert layer as empty.
        with self.assertRaises(SystemExit) as cm:
            self._run(0, "# nothing here yet\n")
        self.assertEqual(cm.exception.code, 2)

    def test_missing_aws_cli_is_fatal_not_a_finding(self):
        # Exit 1 means "found a leak" and warn mode softens it to green; a
        # scanner that could not even start must exit 2.
        with mock.patch("check_private_leaks.subprocess.run",
                        side_effect=FileNotFoundError("aws")):
            with self.assertRaises(SystemExit) as cm:
                load_ssm_deny_list("/some/param")
        self.assertEqual(cm.exception.code, 2)


    def test_aws_failure_is_fatal(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(255, "", "AccessDenied")
        self.assertEqual(cm.exception.code, 2)

    def test_empty_parameter_is_fatal(self):
        # A wiped or typo'd parameter must not read as "nothing to deny".
        with self.assertRaises(SystemExit) as cm:
            self._run(0, "\n")
        self.assertEqual(cm.exception.code, 2)


    def test_undecodable_cli_output_is_fatal_not_a_finding(self):
        err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch("check_private_leaks.subprocess.run", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                load_ssm_deny_list("/some/param")
        self.assertEqual(cm.exception.code, 2)


class DenyRuleAnchoring(unittest.TestCase):
    def test_short_name_does_not_fire_inside_a_word(self):
        self.assertIsNone(deny_rule("ada").search("flights to canada"))

    def test_short_name_fires_as_a_word(self):
        self.assertIsNotNone(deny_rule("ada").search("thanks, Ada."))

    def test_path_prefix_matches_mid_path(self):
        # Edge is `/`, so no boundary is added on that side.
        self.assertIsNotNone(deny_rule("/home/ada").search("x=/home/ada/src"))  # leak-guard-allow: fixture

    def test_domain_suffix_matches_mid_hostname(self):
        self.assertIsNotNone(deny_rule(".example.net").search("h1.example.net"))

    def test_term_is_regex_escaped(self):
        self.assertIsNone(deny_rule("a.b").search("axb"))


class DenyListRedaction(unittest.TestCase):
    """Findings print to a PUBLIC log. A deny-list hit must never echo the
    protected term there; a shape hit still shows its match."""

    def test_deny_hit_is_redacted(self):
        rules = RULES + [("deny-list", deny_rule("zyxwv"), "denied")]
        out = scan("hello zyxwv here", rules, label="t")
        self.assertEqual(len(out), 1)
        self.assertNotIn("zyxwv", out[0].lower())
        self.assertIn("<redacted, 5 chars>", out[0])

    def test_shape_hit_is_still_shown(self):
        out = hits("host 10.1.2.3")  # leak-guard-allow: fixture
        self.assertIn("10.1.2.3", out[0])  # leak-guard-allow: fixture


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



class TreeMode(unittest.TestCase):
    """--tree: the scheduled sweep of every tracked file. Same exit-code
    contract as the diff and text modes, plus: every hit is redacted, and a
    sweep that listed nothing fails closed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self._git("init", "-q", "-b", "main")

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True, env=env).stdout

    def _commit(self, files: dict[str, bytes]) -> None:
        for name, data in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "c")

    def _cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--tree", *args],
                              cwd=cwd or self.repo, capture_output=True, text=True)

    def test_clean_tree_exits_0_and_says_what_it_scanned(self):
        self._commit({"a.md": b"nothing here\n", "b/c.py": b"x = 1\n"})
        out = self._cli()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("2 tracked files", out.stdout)

    def test_existing_leak_exits_1_with_its_match_redacted(self):
        self._commit({"docs/notes.md": b"ok\nthe box at 10.9.8.7 is up\n"})  # leak-guard-allow: fixture
        out = self._cli()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("docs/notes.md:2", out.stderr)
        self.assertIn("rfc1918-ip", out.stderr)
        self.assertIn("<redacted, 8 chars>", out.stderr)
        self.assertNotIn("10.9.8.7", out.stdout + out.stderr)  # leak-guard-allow: fixture

    def test_file_name_is_scanned_too(self):
        self._commit({"srv-thing-01.md": b"clean\n"})  # leak-guard-allow: fixture
        out = self._cli()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("(path)", out.stderr)
        self.assertIn("server-hostname", out.stderr)
        self.assertNotIn("srv-thing-01", out.stderr)  # leak-guard-allow: fixture

    def test_binary_file_is_skipped_and_counted(self):
        self._commit({"img.bin": b"\x00\x01 10.9.8.7", "a.md": b"fine\n"})  # leak-guard-allow: fixture
        out = self._cli()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("1 binary files skipped", out.stdout)

    def test_allow_marker_still_applies(self):
        self._commit({"a.md": b"10.9.8.7 leak-guard-allow: documented\n"})  # leak-guard-allow: fixture
        self.assertEqual(self._cli().returncode, 0)

    def test_deny_list_hit_is_found_and_redacted(self):
        self._commit({"a.md": b"ping zyxwv now\n"})
        completed = subprocess.CompletedProcess([], 0, stdout="zyxwv\n", stderr="")
        # The CLI reads SSM via the aws CLI; exercise the in-process path.
        with mock.patch("check_private_leaks.subprocess.run", return_value=completed):
            deny = load_ssm_deny_list("/x")
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            from check_private_leaks import scan_tree
            found, _lines, _bins = scan_tree(["a.md"], RULES + deny)
        finally:
            os.chdir(cwd)
        self.assertEqual(len(found), 1)
        self.assertNotIn("zyxwv", found[0])
        self.assertIn("<redacted, 5 chars>", found[0])

    def test_outside_a_git_repo_exits_2(self):
        with tempfile.TemporaryDirectory() as bare:
            out = self._cli(cwd=Path(bare))
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("FATAL", out.stderr)

    def test_repo_with_no_tracked_files_exits_2(self):
        out = self._cli()
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("no files", out.stderr)

    def test_tree_is_exclusive_with_other_sources(self):
        self._commit({"a.md": b"x\n"})
        out = self._cli("--text-file", "a.md")
        self.assertEqual(out.returncode, 2)

if __name__ == "__main__":
    unittest.main()
# fmt: on
