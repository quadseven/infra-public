#!/usr/bin/env python3
"""Generate docs/reusables.md, the inputs reference for every reusable
workflow this repo ships, and check that it is current (infra-public#137).

A reusable is any `.github/workflows/*.yml` whose `on:` block declares
`workflow_call:`. For each one the reference lists its inputs, secrets and
outputs exactly as the workflow declares them, plus the pull-request
workflows in this repo that run it, directly or through another reusable.
The workflow file stays the single source; this page is derived from it, and
CI fails when the two disagree, so the page cannot drift the way a
hand-written table would.

Python stdlib only. The parser reads the `workflow_call` block in the shape
every workflow here uses (entries at six spaces, fields at eight, block
scalars deeper) and fails loudly on anything else rather than guessing.

Run:
  python3 .github/scripts/reusable_docs.py           # rewrite the page
  python3 .github/scripts/reusable_docs.py --check   # exit 1 if stale
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WF_DIR = Path(".github") / "workflows"
DOC = Path("docs") / "reusables.md"
REPO_SLUG = "quadseven/infra-public"

SECTIONS = ("inputs", "secrets", "outputs")
FIELDS = ("description", "required", "type", "default", "value")
ENTRY_RE = re.compile(r"^ {6}([A-Za-z_][A-Za-z0-9_-]*):\s*(#.*)?$")
FIELD_RE = re.compile(r"^ {8}([a-z-]+):\s*(.*)$")
SECTION_RE = re.compile(r"^ {4}([a-z_]+):\s*(#.*)?$")
# A call to one of this repo's reusables: local (`./.github/workflows/x.yml`)
# or pinned to this repo by slug (`quadseven/infra-public/...x.yml@sha`).
CALL_RE = re.compile(
    r"^\s*(?:-\s+)?uses:\s*['\"]?(?:\.|" + re.escape(REPO_SLUG) + r")/\.github/workflows/([A-Za-z0-9._-]+\.yml)"
)
TEST_GLOBS = ("*_test.py", "*.test.js")
NAME_RE = re.compile(r"^name:\s*(.*)$")
BLOCK_RE = re.compile(r"^[|>][+-]?\s*$")

# Non-ASCII that appears in workflow text, spelled in ASCII for the page.
# Anything else non-ASCII is an error, so the page stays ASCII.
ASCII_FOR = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "·": "-",  # middle dot
    "→": "->",  # rightwards arrow
    "←": "<-",
    "─": "-",  # box drawing horizontal
    "✓": "ok",  # check mark
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
}


class ParseError(Exception):
    """A workflow_call block in a shape this parser does not read."""


@dataclass
class Reusable:
    path: str
    name: str
    sections: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    exercised_by: list[str] = field(default_factory=list)
    tested_by: list[str] = field(default_factory=list)


def to_ascii(text: str, where: str) -> str:
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in ASCII_FOR:
            out.append(ASCII_FOR[ch])
        else:
            raise ParseError(f"{where}: non-ASCII {ch!r} (U+{ord(ch):04X}) has no ASCII spelling")
    return "".join(out)


def scalar(raw: str) -> str:
    """A one-line YAML scalar as a reader means it."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return inner.replace('\\"', '"') if text[0] == '"' else inner.replace("''", "'")
    # An unquoted scalar may carry a trailing comment.
    return re.sub(r"\s+#.*$", "", text)


def workflow_call_block(lines: list[str]) -> list[str] | None:
    """The lines under `on:` -> `workflow_call:`, or None if it has none."""
    try:
        on = next(i for i, ln in enumerate(lines) if re.match(r"^on:\s*(#.*)?$", ln))
    except StopIteration:
        return None
    end = next(
        (i for i in range(on + 1, len(lines)) if lines[i].strip() and not lines[i][:1].isspace()
         and not lines[i].startswith("#")),
        len(lines),
    )
    block = lines[on + 1 : end]
    try:
        start = next(i for i, ln in enumerate(block) if re.match(r"^ {2}workflow_call:\s*(#.*)?$", ln))
    except StopIteration:
        return None
    stop = next(
        (i for i in range(start + 1, len(block)) if re.match(r"^ {2}\S", block[i]) and not block[i].strip().startswith("#")),
        len(block),
    )
    return block[start + 1 : stop]


def parse_sections(block: list[str], where: str) -> dict[str, dict[str, dict[str, str]]]:
    sections: dict[str, dict[str, dict[str, str]]] = {}
    section: str | None = None
    entry: dict[str, str] | None = None
    i = 0
    while i < len(block):
        line = block[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if m := SECTION_RE.match(line):
            section = m.group(1)
            if section not in SECTIONS:
                raise ParseError(f"{where}: unknown workflow_call section {section!r}")
            sections[section] = {}
            entry = None
        elif m := ENTRY_RE.match(line):
            if section is None:
                raise ParseError(f"{where}: entry {m.group(1)!r} outside inputs/secrets/outputs")
            entry = sections[section].setdefault(m.group(1), {})
        elif m := FIELD_RE.match(line):
            if entry is None:
                raise ParseError(f"{where}: field {m.group(1)!r} outside an entry: {stripped}")
            key, raw = m.group(1), m.group(2)
            if key not in FIELDS:
                raise ParseError(f"{where}: unknown field {key!r}")
            if BLOCK_RE.match(raw.strip()):
                body = []
                while i + 1 < len(block) and (not block[i + 1].strip() or block[i + 1].startswith(" " * 10)):
                    i += 1
                    body.append(block[i].strip())
                entry[key] = " ".join(part for part in body if part)
            else:
                entry[key] = scalar(raw)
        else:
            raise ParseError(f"{where}: unreadable line in workflow_call: {stripped}")
        i += 1
    return sections


def is_pull_request_workflow(lines: list[str]) -> bool:
    try:
        on = next(i for i, ln in enumerate(lines) if re.match(r"^on:", ln))
    except StopIteration:
        return False
    if "pull_request" in re.split(r"[\s,\[\]]+", lines[on][3:]):
        return True
    for line in lines[on + 1 :]:
        if line.strip() and not line[:1].isspace() and not line.startswith("#"):
            return False
        if re.match(r"^ {2}pull_request:", line):
            return True
    return False


def load(root: Path) -> list[Reusable]:
    texts = {p.name: p.read_text() for p in sorted((root / WF_DIR).glob("*.yml"))}
    calls = {
        name: sorted({m.group(1) for ln in text.splitlines() if (m := CALL_RE.match(ln))})
        for name, text in texts.items()
    }
    reusables = []
    for name, text in texts.items():
        lines = text.splitlines()
        block = workflow_call_block(lines)
        if block is None:
            continue
        where = f"{WF_DIR / name}"
        title = next((scalar(m.group(1)) for ln in lines if (m := NAME_RE.match(ln))), name)
        reusables.append(Reusable(where, to_ascii(title, where), parse_sections(block, where)))
    pr_workflows = [n for n, t in texts.items() if is_pull_request_workflow(t.splitlines())]
    scripts = root / ".github" / "scripts"
    tests = {
        str(p.relative_to(root)): p.read_text()
        for pattern in TEST_GLOBS
        for p in sorted(scripts.rglob(pattern))
        if "fixtures" not in p.relative_to(scripts).parts
    }
    for reusable in reusables:
        target = Path(reusable.path).name
        reusable.exercised_by = sorted(w for w in pr_workflows if target in reachable(w, calls))
        reusable.tested_by = sorted(t for t, text in tests.items() if target in text)
    return reusables


def reachable(start: str, calls: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    todo = list(calls.get(start, []))
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(calls.get(name, []))
    return seen


def cell(text: str, where: str) -> str:
    text = to_ascii(text, where).replace("|", "\\|")
    return " ".join(text.split()) or " "


def code(text: str) -> str:
    return f"`{text}`" if text else ""


def render(reusables: list[Reusable]) -> str:
    out = [
        "# Reusable workflows: inputs reference",
        "",
        "<!-- Generated by .github/scripts/reusable_docs.py from each workflow's",
        "     workflow_call block. Do not edit by hand: run the script and commit",
        "     the result. CI fails when this page and the workflows disagree. -->",
        "",
        "Every reusable workflow this repository ships, with the inputs, secrets",
        "and outputs it declares. Call one pinned to a full commit SHA:",
        "",
        "```yaml",
        "jobs:",
        "  example:",
        f"    uses: {REPO_SLUG}/.github/workflows/<file>@<commit-sha>",
        "```",
        "",
        "Each workflow's header comment is the longer explanation. \"Run on PRs by\"",
        "names the pull-request workflows here that call it, directly or through",
        "another reusable; those are also worked examples of a caller. \"Tested by\"",
        "names the test files under `.github/scripts/` that exercise its text.",
        "",
        "| Workflow | Run on PRs by | Tested by |",
        "| --- | --- | --- |",
    ]
    for r in reusables:
        anchor = re.sub(r"[^a-z0-9-]", "", Path(r.path).name.lower().replace(".", ""))
        ran = ", ".join(code(w) for w in r.exercised_by) or "-"
        tested = ", ".join(code(Path(t).name) for t in r.tested_by) or "-"
        out.append(f"| [`{Path(r.path).name}`](#{anchor}) | {ran} | {tested} |")
    for r in reusables:
        where = r.path
        out += ["", f"## {Path(r.path).name}", "", f"{cell(r.name, where)}. Source: [`{r.path}`](../{r.path}).", ""]
        ran = ", ".join(code(w) for w in r.exercised_by) or "nothing in this repository"
        tested = ", ".join(code(t) for t in r.tested_by) or "nothing in this repository"
        out += [f"Run on PRs by: {ran}. Tested by: {tested}.", ""]
        inputs = r.sections.get("inputs", {})
        if inputs:
            out += ["### Inputs", "", "| Name | Type | Required | Default | Description |", "| --- | --- | --- | --- | --- |"]
            for name, f in inputs.items():
                req = "yes" if f.get("required", "false") == "true" else "no"
                default = code(cell(f["default"], where)) if "default" in f else ""
                out.append(
                    f"| `{name}` | {f.get('type', '')} | {req} | {default} | {cell(f.get('description', ''), where)} |"
                )
            out.append("")
        secrets = r.sections.get("secrets", {})
        if secrets:
            out += ["### Secrets", "", "| Name | Required | Description |", "| --- | --- | --- |"]
            for name, f in secrets.items():
                req = "yes" if f.get("required", "false") == "true" else "no"
                out.append(f"| `{name}` | {req} | {cell(f.get('description', ''), where)} |")
            out.append("")
        outputs = r.sections.get("outputs", {})
        if outputs:
            out += ["### Outputs", "", "| Name | Description |", "| --- | --- |"]
            for name, f in outputs.items():
                out.append(f"| `{name}` | {cell(f.get('description', ''), where)} |")
            out.append("")
        if not (inputs or secrets or outputs):
            out += ["Takes no inputs, secrets or outputs.", ""]
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the page is stale")
    args = parser.parse_args(argv)
    try:
        reusables = load(REPO_ROOT)
        page = render(reusables)
    except ParseError as exc:
        print(f"::error::reusable_docs: {exc}")
        return 1
    if not reusables:
        print("::error::reusable_docs: found no workflow_call workflows - the parser is wrong")
        return 1
    target = REPO_ROOT / DOC
    if args.check:
        current = target.read_text() if target.is_file() else ""
        if current != page:
            print(
                f"::error::{DOC} is stale against the workflows. Run "
                f"`python3 .github/scripts/reusable_docs.py` and commit the result."
            )
            return 1
        print(f"reusable_docs: {DOC} is current ({len(reusables)} reusables)")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page)
    print(f"reusable_docs: wrote {DOC} ({len(reusables)} reusables)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
