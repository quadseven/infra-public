#!/usr/bin/env python3
"""Fixture suite for validation_contract.py, plus a check of this repo's own
.loom/validation.conf (infra-public#120).

Two jobs, deliberately kept in one file because they are one contract:

1. Fixtures. A short contract text and a short workflow text in memory, so a
   change to the parser or to the step matcher is proven against cases that
   must be accepted and cases that must be rejected. The direction that
   matters is the rejecting one: a contract that accepts a command no
   pull-request workflow runs is exactly the drift #120 exists to catch, and
   it is invisible in review because the file reads fine.

2. The real .loom/validation.conf, checked against the real workflows. This
   is the test that fails when a check is added to CI without being added to
   the contract, or when a contract entry names a step that was renamed.

Executing the contract's commands is NOT done here - that is
validation_contract.py --run, so the fixture suite stays fast and offline.
Running it in CI is what makes "the contract passes" a checked fact instead
of a claim (see .github/workflows/check.loom-validation.yml).

Python stdlib only (unittest) - no pytest/uv dependency, matching the other
guard suites in this directory.

Run: python3 .github/scripts/validation_contract_test.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from validation_contract import (
    REPO_ROOT,
    ContractEntry,
    check_order,
    check_repository,
    is_pull_request_workflow,
    parse_contract,
    parse_steps,
    run_commands,
)

# A pull-request workflow with one named run step, an inline `run:`, in the
# shape every check.*.yml here uses.
WORKFLOW = """\
name: check - example
on:
  pull_request:
    paths:
      - ".github/scripts/example.py"

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd  # v5.0.1
        with:
          persist-credentials: false
      - name: "Lint the tree"
        run: python3 .github/scripts/example.py
      - name: Block script
        run: |
          set -euo pipefail
          python3 .github/scripts/example.py --strict
"""

CONTRACT = """\
# A comment, ignored.
lint | python3 .github/scripts/example.py | .github/workflows/check.example.yml | Lint the tree

test | python3 .github/scripts/example.py --strict | .github/workflows/check.example.yml | Block script
"""


def write_workflow(root: Path, name: str, text: str) -> None:
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows" / name).write_text(text)


class ParseContract(unittest.TestCase):
    def test_comment_and_blank_lines_are_skipped(self):
        entries, errors = parse_contract(CONTRACT)
        self.assertEqual(errors, [])
        self.assertEqual([e.kind for e in entries], ["lint", "test"])
        self.assertEqual(entries[0].lineno, 2)
        self.assertEqual(entries[0].workflow, ".github/workflows/check.example.yml")
        self.assertEqual(entries[1].step, "Block script")

    def test_fields_are_stripped_of_surrounding_space(self):
        text = "  lint  |  python3 x.py  |  .github/workflows/a.yml  |  Step name  \n"
        entries, errors = parse_contract(text)
        self.assertEqual(errors, [])
        self.assertEqual(entries[0].command, "python3 x.py")
        self.assertEqual(entries[0].step, "Step name")

    def test_unknown_kind_is_rejected(self):
        _, errors = parse_contract("vet | python3 x.py | a.yml | Step\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown kind", errors[0])
        self.assertIn("vet", errors[0])

    def test_wrong_field_count_is_rejected(self):
        for text in ("lint | python3 x.py\n", "lint | a | b | c | d\n"):
            with self.subTest(text=text):
                _, errors = parse_contract(text)
                self.assertEqual(len(errors), 1)
                self.assertIn("4 fields", errors[0])

    def test_empty_field_is_rejected(self):
        _, errors = parse_contract("lint |  | a.yml | Step\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("empty field", errors[0])

    def test_empty_contract_is_rejected(self):
        # A contract with no commands reads as "nothing to run" and would let
        # a lane open a pull request with no checks at all, so say so loudly.
        _, errors = parse_contract("# nothing here\n\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("no commands", errors[0])

    def test_every_error_carries_its_line_number(self):
        text = "lint | a | b\nvet | a | b | c\n"
        _, errors = parse_contract(text)
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].startswith("1:"))
        self.assertTrue(errors[1].startswith("2:"))


class ContractOrder(unittest.TestCase):
    def _order_errors(self, kinds: list[str]) -> list[str]:
        lines = [
            f"{kind} | cmd{i} | .github/workflows/a.yml | Step\n"
            for i, kind in enumerate(kinds)
        ]
        entries, _ = parse_contract("".join(lines))
        return check_order(entries)

    def test_canonical_order_passes(self):
        self.assertEqual(self._order_errors(["lint", "format", "type", "test"]), [])

    def test_repeated_kind_passes(self):
        self.assertEqual(self._order_errors(["lint", "lint", "test", "test"]), [])

    def test_kind_order_is_enforced(self):
        errors = self._order_errors(["test", "lint"])
        self.assertEqual(len(errors), 1)
        self.assertIn("lint", errors[0])
        self.assertIn("after", errors[0])

    def test_kind_may_not_return_after_a_later_kind(self):
        errors = self._order_errors(["test", "lint", "test"])
        self.assertEqual(len(errors), 1)
        self.assertIn("test", errors[0])


class StepParsing(unittest.TestCase):
    def test_inline_and_block_runs_are_both_captured(self):
        steps = parse_steps(WORKFLOW)
        self.assertEqual(
            [name for name, _, _ in steps],
            ["Lint the tree", "Block script"],
        )
        self.assertEqual(steps[0][1], "python3 .github/scripts/example.py")
        self.assertIn("--strict", steps[1][1])

    def test_a_with_block_key_named_name_is_not_a_step_name(self):
        text = (
            WORKFLOW
            + """\
      - name: Call the reusable
        uses: ./.github/workflows/other.yml
        with:
          name: not-a-step-name
"""
        )
        steps = parse_steps(text)
        self.assertNotIn("not-a-step-name", [name for name, _, _ in steps])

    def test_pull_request_trigger_detection(self):
        self.assertTrue(is_pull_request_workflow(WORKFLOW))
        self.assertTrue(is_pull_request_workflow("on: [pull_request]\n"))
        self.assertFalse(
            is_pull_request_workflow("on:\n  push:\n    branches: [main]\n")
        )
        # A `pull_request_target:` sibling is a different trigger.
        self.assertFalse(
            is_pull_request_workflow(
                "on:\n  pull_request_target:\n    types: [opened]\n"
            )
        )


class EntryValidation(unittest.TestCase):
    """Each fixture asserts on the error text, so a validator that stops
    complaining fails here rather than going quietly permissive."""

    def _errors(self, contract: str, *workflows: tuple[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in workflows:
                write_workflow(root, name, text)
            entries, parse_errors = parse_contract(contract)
            return parse_errors + check_repository(entries, root)

    def test_matching_command_passes(self):
        self.assertEqual(self._errors(CONTRACT, ("check.example.yml", WORKFLOW)), [])

    def test_missing_workflow_is_reported(self):
        # One error per entry, so both of the fixture's commands are named -
        # a lane needs to know which lines are affected, not just that
        # something is.
        errors = self._errors(CONTRACT)
        self.assertEqual(len(errors), 2)
        self.assertIn("no such workflow", errors[0])
        self.assertIn("check.example.yml", errors[0])
        self.assertIn("check.example.yml", errors[1])

    def test_workflow_without_a_pull_request_trigger_is_reported(self):
        text = WORKFLOW.replace("  pull_request:", "  workflow_call:")
        errors = self._errors(CONTRACT, ("check.example.yml", text))
        self.assertEqual(len(errors), 2)
        self.assertIn("not triggered by pull_request", errors[0])
        self.assertIn("not triggered by pull_request", errors[1])

    def test_unknown_step_is_reported(self):
        contract = CONTRACT.replace("Lint the tree", "Lint the tr33")
        errors = self._errors(contract, ("check.example.yml", WORKFLOW))
        self.assertEqual(len(errors), 1)
        self.assertIn("no step named", errors[0])
        self.assertIn("Lint the tr33", errors[0])

    def test_command_absent_from_the_step_is_reported(self):
        # The step exists, the command does not: the contract has drifted
        # from CI while still naming a real step.
        contract = CONTRACT.replace(
            "python3 .github/scripts/example.py --strict", "pytest -q"
        )
        errors = self._errors(contract, ("check.example.yml", WORKFLOW))
        self.assertEqual(len(errors), 1)
        self.assertIn("does not run", errors[0])

    def test_step_working_directory_is_refused_not_guessed(self):
        text = WORKFLOW.replace(
            "        run: python3 .github/scripts/example.py\n",
            "        working-directory: python/pkg\n"
            "        run: python3 .github/scripts/example.py\n",
        )
        errors = self._errors(CONTRACT, ("check.example.yml", text))
        self.assertEqual(len(errors), 1)
        self.assertIn("working-directory", errors[0])
        self.assertIn("python/pkg", errors[0])

    def test_quoting_does_not_decide_the_match(self):
        # `"Lint the tree"` and `Lint the tree` are the same YAML scalar, and
        # this repo's steps are written both ways, so neither spelling may be
        # the only one that resolves.
        text = WORKFLOW.replace('- name: "Lint the tree"', "- name: Lint the tree")
        self.assertEqual(self._errors(CONTRACT, ("check.example.yml", text)), [])


class RunCommands(unittest.TestCase):
    def test_passing_command_produces_no_error(self):
        entries = [
            ContractEntry(
                1, "test", 'python3 -c "raise SystemExit(0)"', "a.yml", "Step"
            )
        ]
        self.assertEqual(run_commands(entries, REPO_ROOT), [])

    def test_failing_command_is_reported_with_its_output(self):
        entries = [
            ContractEntry(
                1,
                "test",
                "python3 -c \"print('nope'); raise SystemExit(3)\"",
                "a.yml",
                "S",
            )
        ]
        errors = run_commands(entries, REPO_ROOT)
        self.assertEqual(len(errors), 1)
        self.assertIn("exit 3", errors[0])
        # The command's own output is what tells the lane why it went red.
        self.assertIn("nope", errors[0])

    def test_command_that_does_not_exist_is_reported(self):
        entries = [
            ContractEntry(1, "test", "definitely-not-a-command --x", "a.yml", "Step")
        ]
        errors = run_commands(entries, REPO_ROOT)
        self.assertEqual(len(errors), 1)
        self.assertIn("exit 127", errors[0])

    def test_hanging_command_is_killed_and_reported(self):
        entries = [ContractEntry(1, "test", "sleep 30", "a.yml", "Step")]
        errors = run_commands(entries, REPO_ROOT, timeout=1)
        self.assertEqual(len(errors), 1)
        self.assertIn("did not finish within 1s", errors[0])


class RepoContract(unittest.TestCase):
    CONTRACT = REPO_ROOT / ".loom" / "validation.conf"

    def _contract(self):
        self.assertTrue(
            self.CONTRACT.is_file(),
            f"{self.CONTRACT} is missing - no validation contract",
        )
        return parse_contract(self.CONTRACT.read_text())

    def test_the_repository_contract_matches_the_repository_workflows(self):
        entries, parse_errors = self._contract()
        self.assertEqual(parse_errors, [], "\n".join(parse_errors))
        errors = check_repository(entries, REPO_ROOT)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_the_contract_names_at_least_one_lint_and_one_test(self):
        entries, errors = self._contract()
        self.assertEqual(errors, [])
        kinds = {entry.kind for entry in entries}
        self.assertIn("lint", kinds)
        self.assertIn("test", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
