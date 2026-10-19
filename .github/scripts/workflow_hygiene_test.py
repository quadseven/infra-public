#!/usr/bin/env python3
"""Fixture suite for workflow_hygiene.py.

Small workflow snippets and recorded canonical verdicts exercise the
ported rules, including job boundaries and repo-wide pin labels.

Python stdlib only (unittest) - no pytest/uv dependency, matching the
script's own zero-dependency design.

Run: python3 .github/scripts/workflow_hygiene_test.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from workflow_hygiene import (
    CANONICAL_RULE_COUNT,
    IMPLEMENTED_RULES,
    SCANNED_TREES,
    SKIPPED_RULES,
    UNSCANNED_TREES,
    lint_branch_switch_local_action,
    lint_curl_timeouts,
    lint_file,
    lint_gh_on_arc,
    lint_job_timeouts,
    lint_pin_labels,
    lint_shell_script,
    lint_targets,
    lint_template_injection,
    main,
    skipped_rules_label,
)

DUMMY = Path("workflows/dummy.yml")


class DocsSyncLeakScan(unittest.TestCase):
    def test_sync_diff_is_scanned_before_push(self):
        workflow = Path(__file__).parents[1] / "workflows/sync.public-repo-docs.yml"
        text = workflow.read_text()
        scan_start = text.index("      - name: Scan sync diff and PR text")
        push = text.index("git push -f origin", scan_start)
        scan_step = text[scan_start:push]
        self.assertIn("check_private_leaks.py", scan_step)
        self.assertIn("--diff", scan_step)
        self.assertIn("--text-file ../pr-text.txt", scan_step)
        prepare_start = text.index("      - name: Prepare sync commit and PR text")
        self.assertIn("cat ../pr-title.txt ../pr-body.txt > ../pr-text.txt", text[prepare_start:scan_start])
        self.assertLess(scan_start, push)


class ShaPinning(unittest.TestCase):
    def _lint(self, text: str) -> list[str]:
        # lint_file() takes a path (it reads the file itself), so exercise
        # it through a real temp file rather than duplicating its logic.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(text)
            path = Path(f.name)
        try:
            return lint_file(path)
        finally:
            path.unlink()

    def test_full_sha_passes(self):
        text = "steps:\n  - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd  # v5\n"
        self.assertEqual(self._lint(text), [])

    def test_floating_tag_fails(self):
        text = "steps:\n  - uses: actions/checkout@v4\n"
        errors = self._lint(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("unpinned action", errors[0])

    def test_eol_major_is_flagged_at_any_comment_precision(self):
        # #72: the comment is the ONLY signal Rule 1b has, and it is written at
        # whatever precision the author felt like. All three of these name the
        # same Node-20 checkout, so all three must fail identically. Before the
        # fix only the bare `# v4` was caught: the capture kept one optional
        # minor and was compared against the bare-major table, so `# v4.2.2`
        # reduced to `v4.2`, missed `{"v4"}`, and passed the gate clean - the
        # live instance being `_reusable.upstream-drift-check.yml` shipping a
        # Node-20 checkout to every consumer.
        sha = "11bd71901bbe5b1630ceea73d27597364c9af683"
        for comment in ("v4", "v4.2", "v4.2.2"):
            with self.subTest(comment=comment):
                text = f"steps:\n  - uses: actions/checkout@{sha}  # {comment}\n"
                errors = self._lint(text)
                self.assertEqual(len(errors), 1, f"`# {comment}` must be flagged")
                self.assertIn("Node-20/EOL", errors[0])
                # The message names the comment as WRITTEN, so the author can
                # grep for the string they actually have to change.
                self.assertIn(f"`{comment}`", errors[0])

    def test_node24_capable_major_is_not_flagged_at_any_precision(self):
        # The negative half of #72: precision must not create false positives
        # either. v5+ is Node-24 for these actions and must stay clean.
        sha = "93cb6efe18208431cddfb8368fd83d5badbf9bfd"
        for comment in ("v5", "v5.0", "v5.0.1"):
            with self.subTest(comment=comment):
                text = f"steps:\n  - uses: actions/checkout@{sha}  # {comment}\n"
                self.assertEqual(self._lint(text), [], f"`# {comment}` must pass")

    def test_setup_node_eol_major_precision_too(self):
        # EOL_MAJOR carries two actions; the reduction must apply to both, not
        # just the one that had the live instance.
        sha = "a0853c24544627f65ddf259abe73b1d18a591444"
        text = f"steps:\n  - uses: actions/setup-node@{sha}  # v4.4.0\n"
        errors = self._lint(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("Node-20/EOL", errors[0])

    def test_node20_majors_of_cache_github_script_and_setup_uv_are_flagged(self):
        # #141: these three were shipped in reusables on Node-20 majors and
        # passed Rule 1b because the table did not know them.
        sha = "0123456789abcdef0123456789abcdef01234567"
        for action, comment in (
            ("actions/cache", "v4.2.3"),
            ("actions/github-script", "v7.0.1"),
            ("astral-sh/setup-uv", "v5"),
            ("astral-sh/setup-uv", "v6.4.0"),
        ):
            with self.subTest(action=action, comment=comment):
                errors = self._lint(f"steps:\n  - uses: {action}@{sha}  # {comment}\n")
                self.assertEqual(len(errors), 1, f"{action} # {comment} must be flagged")
                self.assertIn("Node-20/EOL", errors[0])

    def test_node24_majors_of_cache_github_script_and_setup_uv_pass(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        for action, comment in (
            ("actions/cache", "v5.1.0"),
            ("actions/github-script", "v8.0.0"),
            ("astral-sh/setup-uv", "v7.6.0"),
        ):
            with self.subTest(action=action, comment=comment):
                self.assertEqual(self._lint(f"steps:\n  - uses: {action}@{sha}  # {comment}\n"), [])

    def test_local_composite_action_is_not_checked(self):
        text = "steps:\n  - uses: ./.github/actions/my-composite\n"
        self.assertEqual(self._lint(text), [])

    def test_double_quoted_unpinned_uses_is_caught(self):
        # CodeRabbit #61: a quoted scalar bypassed USES_RE entirely before.
        text = 'steps:\n  - uses: "actions/checkout@v4"\n'
        errors = self._lint(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("unpinned action", errors[0])

    def test_single_quoted_pinned_uses_passes(self):
        text = "steps:\n  - uses: 'actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd'  # v5\n"
        self.assertEqual(self._lint(text), [])


class CurlTimeouts(unittest.TestCase):
    def test_curl_with_both_flags_passes(self):
        lines = [
            "run: |",
            '  curl --max-time 30 --connect-timeout 10 "$URL"',
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(errors, [])

    def test_curl_missing_both_flags_fails(self):
        lines = [
            "run: |",
            '  curl "$URL"',
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(len(errors), 1)
        self.assertIn("curl missing", errors[0])

    def test_folded_block_scalar_curl_is_checked(self):
        # CodeRabbit #61: `run: >` (folded) was invisible to the old
        # `run: |`-only block detector.
        lines = [
            "run: >",
            '  curl "$URL"',
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(len(errors), 1)

    def test_folded_block_scalar_with_chomping_indicator_passes_when_safe(self):
        lines = [
            "run: >-",
            '  curl --max-time 30 --connect-timeout 10 "$URL"',
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(errors, [])

    def test_single_line_run_curl_is_checked(self):
        # CodeRabbit #61: `run: curl ...` on one line was invisible - only
        # multi-line `run: |` blocks were scanned. This exact shape (a
        # bare single-line `run:`) already exists 7x in this repo today.
        line = 'run: curl "$URL"'
        errors = lint_curl_timeouts(DUMMY, line)
        self.assertEqual(len(errors), 1)

    def test_single_line_run_non_curl_is_not_flagged(self):
        # A single-line run: that isn't a curl call (e.g. a templated
        # test-cmd input) must not be misdetected.
        line = "run: ${{ inputs.test-cmd }}"
        errors = lint_curl_timeouts(DUMMY, line)
        self.assertEqual(errors, [])

    def test_bare_block_opener_line_itself_is_not_treated_as_inline(self):
        # `run: |` alone (no trailing content) must be recognized as a
        # block opener, not misparsed as an inline scalar whose "command"
        # is the literal pipe character.
        lines = ["run: |", "  echo hi"]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(errors, [])

    def test_curl_in_description_prose_is_not_flagged(self):
        # A `description:` field mentioning curl is YAML metadata text, not
        # shell code - it must not be scoped into a `run: |` block.
        lines = [
            "inputs:",
            "  foo:",
            '    description: "runs curl --max-time under the hood"',
            "run: |",
            "  echo hi",
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(errors, [])

    def test_allow_marker_on_curl_line_suppresses(self):
        lines = [
            "run: |",
            '  curl "$URL" # hygiene: allow-curl-no-timeout flags live in $ARGS',
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(errors, [])

    def test_allow_marker_several_comment_lines_above_suppresses(self):
        # The marker doesn't have to be the literal line directly above -
        # a wrapped multi-line reason comment is legitimate (regression
        # test for the smoke-test.yml false positive this rule originally hit).
        lines = [
            "run: |",
            "  # hygiene: allow-curl-no-timeout both flags are set in args[]",
            "  # above - a static linter can't trace the array back to",
            "  # this call.",
            '  resp=$(curl "${args[@]}")',
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(errors, [])

    def test_allow_marker_scan_stops_at_non_comment_line(self):
        # A marker separated from the curl call by a real code line must
        # NOT suppress - the scan-upward-through-comments logic should
        # stop at the first non-comment line.
        lines = [
            "run: |",
            "  # hygiene: allow-curl-no-timeout unrelated call above",
            "  echo unrelated",
            '  curl "$URL"',
        ]
        errors = lint_curl_timeouts(DUMMY, "\n".join(lines))
        self.assertEqual(len(errors), 1)


class SetE(unittest.TestCase):
    def test_set_e_present_passes(self):
        self.assertEqual(
            lint_shell_script(Path("x.sh"), "#!/bin/sh\nset -euo pipefail\necho hi\n"),
            [],
        )

    def test_missing_set_e_fails(self):
        errors = lint_shell_script(Path("x.sh"), "#!/bin/sh\necho hi\n")
        self.assertEqual(len(errors), 1)

    def test_allow_marker_suppresses(self):
        text = "#!/bin/sh\n# hygiene: allow-no-set-e this script is intentionally best-effort\necho hi\n"
        self.assertEqual(lint_shell_script(Path("x.sh"), text), [])

    def test_shebang_dash_e_passes(self):
        # CodeRabbit #61: the rule's own doc claims `bash -e` satisfies it,
        # but the regex only matched an explicit `set -e` line - this
        # pins the fix.
        self.assertEqual(
            lint_shell_script(Path("x.sh"), "#!/bin/bash -e\necho hi\n"), []
        )

    def test_shebang_without_dash_e_still_fails(self):
        self.assertEqual(
            len(lint_shell_script(Path("x.sh"), "#!/bin/bash\necho hi\n")), 1
        )


class JobTimeouts(unittest.TestCase):
    def test_job_with_timeout_as_second_key_passes(self):
        # Regression test for the MULTILINE anchor bug: timeout-minutes:
        # sitting after runs-on: (i.e. NOT the first line of the job's
        # block) must still be detected.
        text = "\n".join(
            [
                "jobs:",
                "  build:",
                "    runs-on: ubuntu-latest",
                "    timeout-minutes: 10",
                "    steps:",
                "      - run: echo hi",
            ]
        )
        self.assertEqual(lint_job_timeouts(DUMMY, text), [])

    def test_job_with_templated_timeout_input_passes(self):
        text = "\n".join(
            [
                "jobs:",
                "  smoke:",
                "    runs-on: ${{ inputs.runner }}",
                "    timeout-minutes: ${{ inputs.timeout-minutes }}",
                "    steps:",
                "      - run: echo hi",
            ]
        )
        self.assertEqual(lint_job_timeouts(DUMMY, text), [])

    def test_job_missing_timeout_fails(self):
        text = "\n".join(
            [
                "jobs:",
                "  build:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - run: echo hi",
            ]
        )
        errors = lint_job_timeouts(DUMMY, text)
        self.assertEqual(len(errors), 1)
        self.assertIn("job `build`", errors[0])

    def test_reusable_caller_job_with_uses_is_exempt(self):
        text = "\n".join(
            [
                "jobs:",
                "  call-check:",
                "    uses: ./.github/workflows/check.python.yml",
                "    with:",
                "      runner: ubuntu-latest",
            ]
        )
        self.assertEqual(lint_job_timeouts(DUMMY, text), [])

    def test_allow_marker_suppresses(self):
        text = "\n".join(
            [
                "jobs:",
                "  build:",
                "    runs-on: ubuntu-latest",
                "    # hygiene: allow-no-timeout-minutes short-lived, self-limiting job",
                "    steps:",
                "      - run: echo hi",
            ]
        )
        self.assertEqual(lint_job_timeouts(DUMMY, text), [])

    def test_two_jobs_only_second_missing_timeout(self):
        text = "\n".join(
            [
                "jobs:",
                "  build:",
                "    runs-on: ubuntu-latest",
                "    timeout-minutes: 10",
                "    steps:",
                "      - run: echo one",
                "  test:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - run: echo two",
            ]
        )
        errors = lint_job_timeouts(DUMMY, text)
        self.assertEqual(len(errors), 1)
        self.assertIn("job `test`", errors[0])


class CurlOutsideRunBlocks(unittest.TestCase):
    # The port used to check curl only inside `run:` scripts of
    # `.yml` files.

    def test_curl_passed_through_an_input_is_checked(self):
        text = "\n".join(
            [
                "jobs:",
                "  deploy:",
                "    uses: ./.github/workflows/_reusable.deploy.yml",
                "    with:",
                "      smoke-cmd: curl -fsS --max-time 10 http://svc.invalid/healthz",
            ]
        )
        errors = lint_curl_timeouts(DUMMY, text)
        self.assertEqual(len(errors), 1, errors)

    def test_lint_file_checks_curl_in_a_workflow(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("steps:\n  - run: curl -fsS https://example.invalid/\n")
            path = Path(f.name)
        try:
            errors = lint_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("curl missing", errors[0])

    def test_lint_file_checks_curl_in_a_shell_script(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("#!/bin/sh\nset -e\ncurl -fsS https://example.invalid/\n")
            path = Path(f.name)
        try:
            errors = lint_file(path)
        finally:
            path.unlink()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("curl missing", errors[0])


# --- Rule 5 parity with the canonical linter -----------------------------
#
# One fixture per case under fixtures/rule5_parity/, each carrying the
# canonical linter's verdict in a `# canonical-rule5-lines:` header (1-based
# lines of the flagged curls, or `none`). The verdicts were recorded by
# running the canonical linter over these files. With CANONICAL_HYGIENE_LINTER
# pointing at its workflow_hygiene.py, the last test re-derives them live, so
# a stale recording is caught too.

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PARITY_DIR = FIXTURES_DIR / "rule5_parity"
# Bumped by hand when a case is added: a corpus that silently loads zero
# cases must fail, never read as clean.
PARITY_CASE_COUNT = 21


def _parity_cases(
    rule: int = 5,
) -> list[tuple[str, str, tuple[int, ...]]]:
    verdict_re = re.compile(
        rf"^# canonical-rule{rule}-lines:\s*(.+?)\s*$", re.MULTILINE
    )
    cases = []
    for p in sorted((FIXTURES_DIR / f"rule{rule}_parity").iterdir()):
        text = p.read_text()
        m = verdict_re.search(text)
        if not m:
            raise ValueError(f"{p.name}: no `# canonical-rule{rule}-lines:` header")
        verdict = m.group(1)
        expected = (
            () if verdict == "none" else tuple(int(x) for x in verdict.split(","))
        )
        cases.append((p.name, text, expected))
    return cases


def _flagged_lines(errors: list[str]) -> tuple[int, ...]:
    return tuple(sorted(int(re.match(r".*?:(\d+):", e).group(1)) for e in errors))


class Rule5Parity(unittest.TestCase):
    def test_the_corpus_loads_every_case(self):
        n = len(_parity_cases())
        print(f"rule 5 parity corpus: {n} case(s)", file=sys.stderr)
        self.assertEqual(n, PARITY_CASE_COUNT)

    def test_the_port_matches_the_recorded_canonical_verdicts(self):
        cases = _parity_cases()
        self.assertTrue(cases, "zero parity cases compared")
        drift = [
            f"{name}: port flags {list(got)}, canonical flags {list(expected)}"
            for name, text, expected in cases
            if (got := _flagged_lines(lint_curl_timeouts(Path(name), text))) != expected
        ]
        print(
            f"rule 5 parity: port vs recorded canonical, {len(cases)} case(s) compared",
            file=sys.stderr,
        )
        self.assertEqual(drift, [], "\n".join(drift))

    def test_the_recorded_verdicts_match_the_live_canonical_linter(self):
        linter = os.environ.get("CANONICAL_HYGIENE_LINTER")
        if not linter:
            self.skipTest(
                "CANONICAL_HYGIENE_LINTER unset - the recorded verdicts stand in"
            )
        spec = importlib.util.spec_from_file_location("canonical_hygiene", linter)
        self.assertTrue(spec and spec.loader, linter)
        canon = importlib.util.module_from_spec(spec)
        # Registered first: the canonical declares a dataclass, and
        # dataclasses looks its module up in sys.modules while building it.
        sys.modules["canonical_hygiene"] = canon
        spec.loader.exec_module(canon)
        cases = _parity_cases()
        self.assertTrue(cases, "zero parity cases compared")
        drift = [
            f"{name}: recorded {list(expected)}, canonical now {list(live)}"
            for name, text, expected in cases
            if (live := _flagged_lines(canon.lint_curl_timeouts(Path(name), text)))
            != expected
        ]
        print(
            f"rule 5 parity: live canonical vs recorded, {len(cases)} case(s) compared",
            file=sys.stderr,
        )
        self.assertEqual(drift, [], "\n".join(drift))


def _load_canonical():
    linter = os.environ.get("CANONICAL_HYGIENE_LINTER")
    if not linter:
        return None
    spec = importlib.util.spec_from_file_location("canonical_hygiene", linter)
    assert spec and spec.loader, linter
    canon = importlib.util.module_from_spec(spec)
    sys.modules["canonical_hygiene"] = canon
    spec.loader.exec_module(canon)
    return canon


def _lint_pin_labels_lines(lint, name: str, text: str) -> tuple[int, ...]:
    """Rule 11 reports by pin, not by line, and reads files from disk: write
    the case out and collect every `name:LINE` site the errors name."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / name
        path.write_text(text)
        errors, _ = lint([path])
    sites = re.findall(re.escape(str(path)) + r":(\d+)", "\n".join(errors))
    return tuple(sorted({int(s) for s in sites}))


RULE_LINTERS = {
    8: lambda name, text, m: _flagged_lines(
        m.lint_branch_switch_local_action(Path(name), text)
    ),
    11: lambda name, text, m: _lint_pin_labels_lines(m.lint_pin_labels, name, text),
    12: lambda name, text, m: _flagged_lines(m.lint_gh_on_arc(Path(name), text)),
}
# Bumped by hand when a case is added, for the same reason as rule 5's.
NEW_RULE_CASE_COUNTS = {8: 8, 11: 7, 12: 8}


class NewRuleParity(unittest.TestCase):
    """Rules 8, 11 and 12 against the canonical linter's recorded verdicts."""

    def test_every_corpus_loads_its_cases_and_flags_something(self):
        for rule, count in NEW_RULE_CASE_COUNTS.items():
            with self.subTest(rule=rule):
                cases = _parity_cases(rule)
                self.assertEqual(len(cases), count)
                self.assertTrue(
                    any(expected for _, _, expected in cases),
                    f"rule {rule} corpus has no violating case",
                )

    def test_the_port_matches_the_recorded_canonical_verdicts(self):
        this = sys.modules["workflow_hygiene"]
        for rule, lint in RULE_LINTERS.items():
            drift = [
                f"{name}: port flags {list(got)}, canonical flags {list(expected)}"
                for name, text, expected in _parity_cases(rule)
                if (got := lint(name, text, this)) != expected
            ]
            self.assertEqual(drift, [], f"rule {rule}:\n" + "\n".join(drift))

    def test_the_recorded_verdicts_match_the_live_canonical_linter(self):
        canon = _load_canonical()
        if canon is None:
            self.skipTest("CANONICAL_HYGIENE_LINTER unset - recorded verdicts stand in")
        for rule, lint in RULE_LINTERS.items():
            drift = [
                f"{name}: recorded {list(expected)}, canonical now {list(live)}"
                for name, text, expected in _parity_cases(rule)
                if (live := lint(name, text, canon)) != expected
            ]
            self.assertEqual(drift, [], f"rule {rule}:\n" + "\n".join(drift))


class SkippedRules(unittest.TestCase):
    def test_rules_2_3_4_10_and_13_are_declared_skipped_with_a_reason(self):
        self.assertEqual(sorted(SKIPPED_RULES), [2, 3, 4, 10, 13])
        self.assertTrue(all(SKIPPED_RULES.values()))

    def test_every_canonical_rule_is_implemented_or_skipped(self):
        # infra-public#125: rules 2 and 13 were in neither list, and a clean
        # run read as having checked them. A number in neither now fails here.
        canonical = set(range(1, CANONICAL_RULE_COUNT + 1))
        implemented, skipped = set(IMPLEMENTED_RULES), set(SKIPPED_RULES)
        self.assertEqual(sorted(canonical - implemented - skipped), [])
        self.assertEqual(sorted(implemented & skipped), [])
        self.assertEqual(sorted((implemented | skipped) - canonical), [])

    def test_every_rule_number_is_named_in_the_linter_source(self):
        src = Path(__file__).with_name("workflow_hygiene.py").read_text()
        missing = [
            n for n in range(1, CANONICAL_RULE_COUNT + 1)
            if not re.search(rf"Rule {n}\b", src)
        ]
        self.assertEqual(missing, [])

    def test_label_names_them_as_the_canonical_linter_does(self):
        self.assertEqual(skipped_rules_label(), "rules 2, 3, 4, 10 and 13 skipped")

    def test_runtime_output_names_the_skipped_rules(self):
        out = io.StringIO()
        before = Path.cwd()
        os.chdir(Path(__file__).parents[2])
        try:
            with contextlib.redirect_stdout(out):
                code = main()
        finally:
            os.chdir(before)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("rules 2, 3, 4, 10 and 13 skipped", out.getvalue())


class BranchSwitchAndGhAndPinLabels(unittest.TestCase):
    def test_rule_8_names_the_local_action_line(self):
        text = "steps:\n  - run: git checkout state\n  - uses: ./.github/actions/x\n"
        errors = lint_branch_switch_local_action(DUMMY, text)
        self.assertEqual(len(errors), 1)
        self.assertIn(":3:", errors[0])
        self.assertIn("Rule 8", errors[0])

    def test_rule_12_names_the_gh_line(self):
        text = "jobs:\n  j:\n    runs-on: arc-x\n    steps:\n      - run: gh pr list\n"
        errors = lint_gh_on_arc(DUMMY, text)
        self.assertEqual(len(errors), 1)
        self.assertIn(":5:", errors[0])
        self.assertIn("Rule 12", errors[0])

    def test_rule_11_names_the_conflicting_labels(self):
        sha = "a" * 40
        text = f"      - uses: o/a@{sha}  # v5\n      - uses: o/a@{sha}  # v4\n"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "w.yml"
            path.write_text(text)
            errors, summary = lint_pin_labels([path])
        self.assertEqual(len(errors), 1)
        self.assertIn("2 version labels", errors[0])
        self.assertIn("Rule 11", errors[0])
        self.assertIn("1 with conflicting labels", summary)

    def test_lint_file_wires_rules_8_and_12_for_workflows(self):
        text = (
            "jobs:\n  j:\n    runs-on: arc-x\n    timeout-minutes: 5\n    steps:\n"
            "      - run: git checkout state\n      - run: gh pr list\n"
            "      - uses: ./.github/actions/x\n"
        )
        before = Path.cwd()
        with tempfile.TemporaryDirectory() as d:
            wf = Path(d) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "w.yml").write_text(text)
            os.chdir(d)
            try:
                errors = lint_file(Path(".github/workflows/w.yml"))
            finally:
                os.chdir(before)
        self.assertEqual(len(errors), 2, errors)
        self.assertTrue(any("local action" in e for e in errors))
        self.assertTrue(any("`gh` in job" in e for e in errors))


class TemplateInjection(unittest.TestCase):
    """Rule 9, ported from the canonical linter 2026-09-22."""

    def _lint(self, text: str) -> list[str]:
        return lint_template_injection(DUMMY, text)

    def test_event_value_in_run_block_fails(self):
        text = (
            "steps:\n"
            "  - run: |\n"
            '      echo "${{ github.event.pull_request.head.ref }}"\n'
        )
        errors = self._lint(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("github.event.pull_request.head.ref", errors[0])
        self.assertIn(":3:", errors[0])

    def test_step_output_in_single_line_run_fails(self):
        text = "steps:\n  - run: echo ${{ steps.tag.outputs.version }}\n"
        self.assertEqual(len(self._lint(text)), 1)

    def test_value_passed_through_env_passes(self):
        # The prescribed fix: the expression lives in env:, the script reads $VAR.
        text = (
            "steps:\n"
            "  - env:\n"
            "      REF: ${{ github.event.pull_request.head.ref }}\n"
            "    run: |\n"
            '      echo "$REF"\n'
        )
        self.assertEqual(self._lint(text), [])

    def test_following_steps_env_is_not_read_as_script(self):
        text = (
            "steps:\n"
            "  - run: |\n"
            "      echo hi\n"
            "  - env:\n"
            "      TITLE: ${{ github.event.pull_request.title }}\n"
            "    run: echo \"$TITLE\"\n"
        )
        self.assertEqual(self._lint(text), [])

    def test_benign_families_are_not_flagged(self):
        text = (
            "steps:\n"
            "  - run: |\n"
            "      echo ${{ matrix.svc }} ${{ github.sha }} ${{ inputs.name }}\n"
        )
        self.assertEqual(self._lint(text), [])

    def test_allow_marker_suppresses(self):
        text = (
            "steps:\n"
            "  - run: |\n"
            '      echo "${{ github.event.number }}"  # hygiene: allow-interpolation numeric id\n'
        )
        self.assertEqual(self._lint(text), [])

    def test_lint_file_wires_rule_9_for_workflows(self):
        text = "steps:\n  - run: echo ${{ github.event.issue.title }}\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(text)
            path = Path(f.name)
        try:
            self.assertTrue(any("github.event.issue.title" in e for e in lint_file(path)))
        finally:
            path.unlink()


# --- File scope, ledgered like the rule numbers (infra-public#156) ---------
#
# The trees the canonical linter's main() scans, recorded from it: workflows
# and composite-action metadata go through lint_file, shell scripts under
# .github/scripts, .github/actions and two canonical-repo deploy trees go
# through the shell and curl checks. Each one must be in exactly one of
# SCANNED_TREES and UNSCANNED_TREES.
CANONICAL_TREES = {
    ".github/workflows/*.yml",
    ".github/actions/**/action.yml",
    ".github/scripts/**/*.sh",
    ".github/actions/**/*.sh",
    "production/**/*.sh",
    "pulumi/**/*.sh",
}

# One file per canonical tree, so lint_targets() can be checked tree by tree
# against the ledger without a glob matcher.
TREE_PROBES = {
    ".github/workflows/w.yml": ".github/workflows/*.yml",
    ".github/actions/a/nested/action.yml": ".github/actions/**/action.yml",
    ".github/scripts/s.sh": ".github/scripts/**/*.sh",
    ".github/actions/a/run.sh": ".github/actions/**/*.sh",
    "production/p.sh": "production/**/*.sh",
    "pulumi/q.sh": "pulumi/**/*.sh",
}

SCOPE_FIXTURE_ROOT = FIXTURES_DIR / "scope_parity"


class ScopeLedger(unittest.TestCase):
    def test_every_canonical_tree_is_scanned_or_unscanned(self):
        scanned, unscanned = set(SCANNED_TREES), set(UNSCANNED_TREES)
        self.assertEqual(sorted(CANONICAL_TREES - scanned - unscanned), [])
        self.assertEqual(sorted(scanned & unscanned), [])
        self.assertEqual(sorted((scanned | unscanned) - CANONICAL_TREES), [])
        self.assertTrue(all(SCANNED_TREES.values()))
        self.assertTrue(all(UNSCANNED_TREES.values()))

    def test_the_probes_cover_every_canonical_tree(self):
        self.assertEqual(set(TREE_PROBES.values()), CANONICAL_TREES)

    def test_lint_targets_reaches_exactly_the_scanned_trees(self):
        # The code side of the ledger: a tree the ledger calls scanned must be
        # reached, and a tree it calls unscanned must not be.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in TREE_PROBES:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("set -e\n")
            fixture_sh = root / ".github/scripts/fixtures/x/bad.sh"
            fixture_sh.parent.mkdir(parents=True)
            fixture_sh.write_text("curl https://example.invalid/\n")
            targets = {p.relative_to(root).as_posix() for p in lint_targets(root)}
        reached = {TREE_PROBES[t] for t in targets if t in TREE_PROBES}
        print(
            f"scope ledger: {len(reached)} of {len(CANONICAL_TREES)} canonical trees reached",
            file=sys.stderr,
        )
        self.assertEqual(sorted(reached), sorted(SCANNED_TREES))
        self.assertEqual(sorted(targets - set(TREE_PROBES)), [])

    def test_the_docs_state_the_scope(self):
        doc = (Path(__file__).parents[2] / "docs/workflow-hygiene.md").read_text()
        for tree in CANONICAL_TREES:
            with self.subTest(tree=tree):
                self.assertIn(f"`{tree}`", doc)


class ScopeParity(unittest.TestCase):
    """A composite action.yml is linted like a workflow, as canonical does."""

    def _recorded(self) -> tuple[Path, tuple[int, ...]]:
        path = SCOPE_FIXTURE_ROOT / ".github/actions/probe/action.yml"
        m = re.search(r"^# canonical-scope-lines:\s*(.+?)\s*$", path.read_text(), re.M)
        self.assertIsNotNone(m, f"{path}: no `# canonical-scope-lines:` header")
        return path, tuple(int(x) for x in m.group(1).split(","))

    def _run_main_in_fixture(self, run) -> str:
        out = io.StringIO()
        before = Path.cwd()
        os.chdir(SCOPE_FIXTURE_ROOT)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.code = run()
        finally:
            os.chdir(before)
        return out.getvalue()

    def test_the_port_lints_the_fixture_action_as_canonical_does(self):
        path, expected = self._recorded()
        targets = lint_targets(SCOPE_FIXTURE_ROOT)
        self.assertEqual(targets, [path])
        errors = [e for t in targets for e in lint_file(t)]
        self.assertEqual(_flagged_lines(errors), expected, errors)
        # Line 10 is Rule 1's message, line 12 is Rule 5's.
        self.assertTrue(any("unpinned action `actions/checkout@v4`" in e for e in errors))
        self.assertTrue(any("curl missing" in e for e in errors))

    def test_main_exits_1_on_the_fixture_tree(self):
        out = self._run_main_in_fixture(main)
        self.assertEqual(self.code, 1, out)
        self.assertIn("2 violation(s) in 1 files checked", out)

    def test_the_recorded_verdict_matches_the_live_canonical_linter(self):
        canon = _load_canonical()
        if canon is None:
            self.skipTest("CANONICAL_HYGIENE_LINTER unset - recorded verdict stands in")
        _, expected = self._recorded()
        out = self._run_main_in_fixture(canon.main)
        errors = [ln for ln in out.splitlines() if ln.startswith("::error::")]
        self.assertEqual(_flagged_lines(errors), expected, out)


if __name__ == "__main__":
    unittest.main()
