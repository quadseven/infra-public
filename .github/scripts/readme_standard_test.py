#!/usr/bin/env python3
"""README.md follows the public README standard (#179).

The standard lives at templates/public-repo/README-STANDARD.md. This suite
checks the parts of its reviewer checklist a machine can check: the opening,
the seven required sections in order, a Mermaid diagram under How it works,
ASCII only, a live status link, a Contributing section that points to
AGENTS.md, and the facts the README must keep.

Python stdlib only (unittest), matching the other guard suites here.

Run: python3 .github/scripts/readme_standard_test.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

REQUIRED = [
    "What it does",
    "Quick start",
    "How it works",
    "Run and test locally",
    "Configuration",
    "Status",
    "Contributing",
]

# Facts the README carried before the standard; rewriting it must not drop them.
KEPT_FACTS = [
    ".loom/validation.conf",
    "python3 .github/scripts/validation_contract.py --run",
    "uv run --frozen pytest -q",
    "docs/reusables.md",
    "full commit SHA",
    "AGENTS.md",
]

BOX_DRAWING = re.compile("[─-╿]")


def sections(text: str) -> dict[str, str]:
    """Map each `## ` heading to the body under it, outside code fences."""
    out: dict[str, str] = {}
    current = None
    fenced = False
    for line in text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = line[3:].strip()
            out[current] = ""
            continue
        if current is not None:
            out[current] += line + "\n"
    return out


class ReadmeStandardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = README.read_text(encoding="utf-8")
        cls.sections = sections(cls.text)

    def test_opening(self) -> None:
        lines = self.text.splitlines()
        self.assertTrue(lines[0].startswith("# "), "line 1 is the # name")
        body = [ln for ln in lines[1:] if ln.strip()]
        self.assertFalse(body[0].startswith("#"), "a description follows")
        self.assertNotIn("![", "\n".join(lines[:5]), "no badge wall")

    def test_required_sections_in_order(self) -> None:
        headings = list(self.sections)
        missing = [h for h in REQUIRED if h not in headings]
        self.assertEqual(missing, [], "missing required sections")
        order = [headings.index(h) for h in REQUIRED]
        self.assertEqual(order, sorted(order), "sections out of order")

    def test_how_it_works_has_mermaid(self) -> None:
        self.assertIn("```mermaid", self.sections.get("How it works", ""))

    def test_ascii_only(self) -> None:
        bad = sorted({c for c in self.text if ord(c) > 127})
        self.assertEqual(bad, [], "non-ASCII characters")
        self.assertIsNone(BOX_DRAWING.search(self.text))

    def test_status_links_one_issue(self) -> None:
        links = re.findall(
            r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+",
            self.sections.get("Status", ""),
        )
        self.assertEqual(len(links), 1, "Status links one epic or goal")

    def test_contributing(self) -> None:
        body = self.sections.get("Contributing", "")
        self.assertIn("pull request", body)
        self.assertIn("AGENTS.md", body)

    def test_kept_facts(self) -> None:
        for fact in KEPT_FACTS:
            with self.subTest(fact=fact):
                self.assertIn(fact, self.text)


if __name__ == "__main__":
    unittest.main()
