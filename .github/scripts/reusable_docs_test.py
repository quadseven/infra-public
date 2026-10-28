#!/usr/bin/env python3
"""Fixture suite for reusable_docs.py (infra-public#137).

Python stdlib only (unittest), matching the other guard suites here.

Run: python3 .github/scripts/reusable_docs_test.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import reusable_docs
from reusable_docs import (
    ParseError,
    load,
    parse_sections,
    reachable,
    render,
    to_ascii,
    workflow_call_block,
)

REUSABLE = """\
name: _reusable - example

# Header comment.

on:
  workflow_call:
    inputs:
      # -- a section comment --
      runner:
        description: "Runner label (e.g. ubuntu-latest)."
        required: false
        type: string
        default: "ubuntu-latest"
      arm64-runner:
        description: |
          A block scalar
          over two lines.
        required: true
        type: string
      push:
        description: 'It''s a boolean'
        required: false
        type: boolean
        default: true
    secrets:
      role_to_assume:
        description: "A | pipe in a cell."
        required: false
    outputs:
      digest:
        description: "The digest."
        value: ${{ jobs.build.outputs.digest }}

permissions:
  contents: read

jobs:
  build:
    runs-on: ${{ inputs.runner }}
    timeout-minutes: 5
    steps:
      - run: echo hi
"""

CALLER = """\
name: canary
on:
  pull_request:
    paths: ["x"]
jobs:
  canary:
    uses: ./.github/workflows/outer.yml
"""

OUTER = """\
name: outer
on:
  workflow_call:
jobs:
  inner:
    uses: quadseven/infra-public/.github/workflows/example.yml@0123456789abcdef0123456789abcdef01234567
"""


def sections(text: str):
    return parse_sections(workflow_call_block(text.splitlines()), "fixture")


class Parsing(unittest.TestCase):
    def test_reads_every_section_and_entry(self):
        got = sections(REUSABLE)
        self.assertEqual(list(got), ["inputs", "secrets", "outputs"])
        self.assertEqual(list(got["inputs"]), ["runner", "arm64-runner", "push"])

    def test_scalars_are_unquoted_and_block_scalars_joined(self):
        inputs = sections(REUSABLE)["inputs"]
        self.assertEqual(inputs["runner"]["default"], "ubuntu-latest")
        self.assertEqual(inputs["arm64-runner"]["description"], "A block scalar over two lines.")
        self.assertEqual(inputs["push"]["description"], "It's a boolean")
        self.assertEqual(inputs["push"]["default"], "true")

    def test_a_workflow_without_workflow_call_is_not_a_reusable(self):
        self.assertIsNone(workflow_call_block(CALLER.splitlines()))

    def test_an_unknown_field_fails_loudly(self):
        bad = REUSABLE.replace("        type: boolean\n", "        typo: boolean\n")
        with self.assertRaises(ParseError):
            sections(bad)

    def test_an_unreadable_line_fails_loudly(self):
        bad = REUSABLE.replace("      push:\n", "     push:\n")
        with self.assertRaises(ParseError):
            sections(bad)


class Ascii(unittest.TestCase):
    def test_known_punctuation_is_spelled_in_ascii(self):
        self.assertEqual(to_ascii("a \u2014 b \u00b7 c \u2192 d", "x"), "a - b - c -> d")

    def test_unknown_non_ascii_is_an_error(self):
        with self.assertRaises(ParseError):
            to_ascii("caf\u00e9", "x")


class Coverage(unittest.TestCase):
    def test_reachable_follows_local_and_slug_pinned_calls(self):
        calls = {"canary.yml": ["outer.yml"], "outer.yml": ["example.yml"]}
        self.assertEqual(reachable("canary.yml", calls), {"outer.yml", "example.yml"})

    def test_a_pr_workflow_reaching_a_reusable_through_another_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf = root / ".github" / "workflows"
            wf.mkdir(parents=True)
            (root / ".github" / "scripts").mkdir()
            (wf / "example.yml").write_text(REUSABLE)
            (wf / "outer.yml").write_text(OUTER)
            (wf / "canary.yml").write_text(CALLER)
            (root / ".github" / "scripts" / "x_test.py").write_text("# reads example.yml\n")
            found = {Path(r.path).name: r for r in load(root)}
        self.assertEqual(sorted(found), ["example.yml", "outer.yml"])
        self.assertEqual(found["example.yml"].exercised_by, ["canary.yml"])
        self.assertEqual(found["example.yml"].tested_by, [".github/scripts/x_test.py"])
        self.assertEqual(found["example.yml"].path, ".github/workflows/example.yml")
        self.assertIn("../.github/workflows/example.yml", render([found["example.yml"]]))
        self.assertEqual(found["outer.yml"].exercised_by, ["canary.yml"])

    def test_rendered_page_escapes_pipes_and_is_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "example.yml").write_text(REUSABLE)
            page = render(load(root))
        self.assertIn("A \\| pipe in a cell.", page)
        self.assertIn("| `arm64-runner` | string | yes |  | A block scalar over two lines. |", page)
        self.assertTrue(page.isascii())


class RepoPage(unittest.TestCase):
    def test_the_committed_page_matches_the_workflows(self):
        self.assertEqual(reusable_docs.main(["--check"]), 0)

    def test_the_readme_names_every_reusable(self):
        # infra-public#138: a reusable missing from the README is one a
        # stranger landing on the repo does not know exists.
        readme = (reusable_docs.REPO_ROOT / "README.md").read_text()
        missing = [
            Path(r.path).name for r in load(reusable_docs.REPO_ROOT)
            if f"`{Path(r.path).name}`" not in readme
        ]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
