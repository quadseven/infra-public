#!/usr/bin/env python3
"""Fixture suite for reusable_docs.py (infra-public#137).

Python stdlib only (unittest), matching the other guard suites here.

Run: python3 .github/scripts/reusable_docs_test.py
"""

from __future__ import annotations

import re
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


POLICY_FILES = ("templates/public-repo/AGENTS.md", "AGENTS.md")

# A sentence that turns repository `permissions` booleans back into an
# access verdict for an installation token: the rule this guard removed.
# A dot followed by a word character (`permissions.push`) is not a
# sentence end.
UNSUPPORTED_INFERENCE = re.compile(
    r"`?permissions(?:[^.]|\.(?=\w))*\b(?:means|shows|proves|indicates|confirms)\b"
    r"(?:[^.]|\.(?=\w))*"
    r"\b(?:access|denied|denial|blocked|cannot write|read-only)\b",
    re.IGNORECASE,
)


def unsupported_inferences(policy: str) -> list[str]:
    """Sentences in `policy` that read permission booleans as an access verdict."""
    flat = " ".join(policy.split())
    return [m.group(0) for m in UNSUPPORTED_INFERENCE.finditer(flat)]


class PublicAgentPolicy(unittest.TestCase):
    def policies(self) -> dict[str, str]:
        root = Path(__file__).resolve().parents[2]
        return {name: " ".join((root / name).read_text().split()) for name in POLICY_FILES}

    def test_installation_token_access_guidance_uses_scope_and_observed_denial(self):
        required = (
            "Installation scope and repository permission booleans are different things.",
            "use the installation's repository scope to establish whether a repository is included",
            "Do not infer the token's capabilities from repository `permissions` booleans",
            "Report that a write is blocked only after the exact authorized command returns a denial response.",
            "State the command and the denial response as evidence.",
            "Do not probe access by creating credentials, changing grants, or falling back to a personal identity.",
            "Use only the dedicated App identity.",
            "Keep credentials out of arguments, output, logs",
        )
        for name, policy in self.policies().items():
            for phrase in required:
                with self.subTest(file=name, phrase=phrase):
                    self.assertIn(phrase, policy)

    def test_policy_does_not_read_permission_booleans_as_an_access_verdict(self):
        for name, policy in self.policies().items():
            with self.subTest(file=name):
                self.assertEqual(unsupported_inferences(policy), [])

    def test_the_inference_check_catches_the_removed_rule(self):
        removed_rule = (
            "Check the repository response first. If `permissions.push` is\n"
            "false, that shows the token is denied write access; report it as blocked."
        )
        self.assertEqual(len(unsupported_inferences(removed_rule)), 1)

    def test_root_agents_shared_section_matches_template(self):
        root = Path(__file__).resolve().parents[2]
        marker = "<!-- repo-specific below -->"
        template = (root / "templates/public-repo/AGENTS.md").read_text()
        synced = (root / "AGENTS.md").read_text()
        self.assertIn(marker, synced)
        self.assertEqual(synced.split(marker)[0], template.split(marker)[0])


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
