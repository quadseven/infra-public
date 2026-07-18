#!/usr/bin/env python3
"""Workflow-hygiene linter for infra-public's own workflows (infra-public#46).

infra-public is the shared reusable-workflow library every fleet repo
inherits via `uses: githumps/infra-public/...`, so drift here is inherited
by every consumer with nothing local to catch it - #18 (Node-20-era action
pins that still passed a SHA-only check) was found and fixed only by a
manual audit, not by any gate.

The canonical rule set lives in `infrastructure/.github/scripts/
workflow_hygiene.py` (the private fleet repo) and has seven rules. Four are
genuinely repo-agnostic and ported here verbatim (same regexes, same
exception-comment conventions, so a contributor who knows one knows both):

  1. SHA-pinning: every third-party `uses:` must be a full 40-hex commit
     SHA, not a floating `@vN`/`@main` tag (supply-chain drift + the Node-20
     EOL problem #18 fixed).
  5. curl timeouts: every real `curl` in a `run:` block needs both a
     total-time bound (--max-time/-m) and a connect bound
     (--connect-timeout). Exception: `# hygiene: allow-curl-no-timeout <reason>`.
  6. set -e in standalone shell scripts: every `.sh` under .github/ must
     enable -e. Exception: `# hygiene: allow-no-set-e <reason>`.
  7. per-job timeout-minutes: every job with `runs-on:` must set
     `timeout-minutes:` (GitHub's 360-min default silently burns runner
     time on a hang). A reusable-workflow CALLER job (top-level `uses:`
     instead of `runs-on:`) is exempt. Exception:
     `# hygiene: allow-no-timeout-minutes <reason>`.

Deliberately NOT ported - infra-specific, would be dead code or actively
wrong here: dead-cluster reference checking (k8s-ts is a private-infra
teardown artifact) and ARC-runner-routing policy (infra-public's own CI
uses plain GitHub-hosted runners, see check.spark-cave.yml - there is no
ARC fleet-routing concept in this repo).

Run locally:  python3 .github/scripts/workflow_hygiene.py
Exit 0 = clean; exit 1 = violations (printed as ::error:: for CI annotation).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

GITHUB_DIR = Path(".github")
WF_DIR = GITHUB_DIR / "workflows"

# Local composites (./.github/actions/...) and same-repo reusables have no
# pinning concern; only owner/repo@ref refs are checked. `['"]?` before the
# owner tolerates a quoted YAML scalar (`uses: "actions/checkout@v4"`) -
# the ref capture group's own charset already excludes the closing quote,
# so no corresponding change is needed on the right side.
USES_RE = re.compile(r"^\s*-?\s*uses:\s*['\"]?([A-Za-z0-9_-]+/[A-Za-z0-9._/-]+)@([A-Za-z0-9._-]+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Rule 5 - curl timeouts. A real `curl` invocation: the token is preceded by
# start-of-string or a shell separator (not part of a longer word like
# `mycurl`) AND immediately followed by an actual argument - a flag (-), a
# URL, a variable, a quote, or a line continuation. This excludes prose
# mentions that live in `name:`/`description:` fields and comments.
CURL_RE = re.compile(r"(?:^|[\s;&|(`$])curl\s+(?:-|\\$|\"|'|\$|https?://)")
CURL_MAXTIME_RE = re.compile(r"(?:--max-time(?:[=\s]|$)|(?:^|\s)-m(?:[=\s]|$))")
CURL_CONNTIMEOUT_RE = re.compile(r"--connect-timeout(?:[=\s]|$)")
CURL_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-curl-no-timeout\b")

# Rule 6 - set -e in standalone shell scripts. `bash -e`/`set -e`/
# `set -euo pipefail` all satisfy it; we only require the -e flag to be on.
SET_E_RE = re.compile(r"^\s*set\s+-[a-z]*e", re.M)
# A shebang carrying -e (`#!/bin/bash -e`, `#!/bin/sh -e`) satisfies the
# rule the same way an explicit `set -e` line does. `\b[a-z]*sh\b`, not
# `\bsh\b` - "bash"/"dash"/"zsh" have no word boundary before "sh" (the
# preceding "a" is itself a word char), only after it.
SHEBANG_E_RE = re.compile(r"^#!.*\b[a-z]*sh\b.*\s-[a-z]*e\b", re.M)
SET_E_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-no-set-e\b")

# Rule 7 - per-job timeout-minutes. Walk the `jobs:` map; a job key sits two
# spaces in (`  build:`) and its keys (`runs-on:`, `uses:`,
# `timeout-minutes:`) sit four spaces in. A job with `runs-on:` needs
# `timeout-minutes:`; a job whose key carries a top-level `uses:` (reusable
# caller) is exempt. Matrix `runs-on: ${{ ... }}` still needs a job timeout.
#
# The 2/4-space indentation is hardcoded, not derived from `jobs:` - a
# deliberate scope limit (CodeRabbit #61 flagged this as a real gap for a
# workflow indented differently). Every one of this repo's 14 workflow
# files already uses standard 2-space indentation, matching this script's
# own regex-based, non-YAML-parsing design; genuinely deriving indentation
# would mean a structural YAML parse for a repo-local convention gate. If
# infra-public ever adopts non-standard indentation, widen this then.
JOBS_HEADER_RE = re.compile(r"^jobs:\s*$")
JOB_KEY_RE = re.compile(r"^\s{2}([A-Za-z0-9_-]+):\s*$")
# re.M: these match with .search() against a multi-line `block` string built
# from several joined lines, not a single line - without MULTILINE, `^` only
# anchors to the start of the whole block, so a key that isn't literally the
# block's first line (e.g. timeout-minutes: sitting after runs-on:) can never
# match even when present.
JOB_RUNS_ON_RE = re.compile(r"^\s{4}runs-on:", re.M)
JOB_USES_RE = re.compile(r"^\s{4}uses:", re.M)
JOB_TIMEOUT_RE = re.compile(r"^\s{4}timeout-minutes:\s*\S", re.M)
TIMEOUT_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-no-timeout-minutes\b")


def code_part(line: str) -> str:
    """Strip a trailing `# comment` for matching purposes, respecting
    quoted strings so a `#` inside a quoted value isn't mistaken for a
    comment marker."""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


# Literal (`|`) or folded (`>`) block scalar, with an optional chomping
# indicator (`-`/`+`). Both are treated identically here - folding only
# changes how newlines are interpreted at execution time, not whether a
# `curl` call inside the block needs its timeout flags.
RUN_BLOCK_RE = re.compile(r"^(\s*)run:\s*[|>][-+]?\s*$")
# A single-line scalar: `run: <command>` with real content on the same
# line (not `|`/`>`, which are handled above as block openers).
RUN_INLINE_RE = re.compile(r"^(\s*)run:\s*(?![|>]([-+]?\s*$))(\S.*)$")


def _run_block_line_numbers(lines: list[str]) -> set[int]:
    """Return the 0-indexed line numbers that contain actual shell code -
    inside a `run: |`/`run: >` YAML block scalar, or on a single-line
    `run: <command>` scalar - as opposed to `name:`/`description:` field
    text elsewhere in the file (a `description:` value can legally contain
    the word "curl" as prose without being a real invocation)."""
    in_block: set[int] = set()
    i = 0
    n = len(lines)
    while i < n:
        m = RUN_BLOCK_RE.match(lines[i])
        if m:
            key_indent = len(m.group(1))
            i += 1
            while i < n and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > key_indent):
                in_block.add(i)
                i += 1
            continue
        if RUN_INLINE_RE.match(lines[i]):
            in_block.add(i)
        i += 1
    return in_block


def lint_curl_timeouts(path: Path, text: str) -> list[str]:
    """Rule 5. For each real `curl` invocation inside a `run:` scalar
    (block `|`/`>` or single-line - never in YAML field text like
    `name:`/`description:`), join continuation lines (trailing `\\`) so
    a multi-line curl's flags are seen together, then require both timeout
    flags unless the allow-marker is present on the curl line or anywhere in
    the contiguous run of comment-only lines directly above it (a wrapped
    multi-line reason is legitimate prose, not just a single fixed line)."""
    errors: list[str] = []
    lines = text.splitlines()
    run_lines = _run_block_line_numbers(lines)
    i = 0
    while i < len(lines):
        line = lines[i]
        code = code_part(line)
        if i in run_lines and CURL_RE.search(code):
            joined = line
            j = i
            while joined.rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                joined += "\n" + lines[j]
            k = i - 1
            comment_block: list[str] = []
            while k >= 0 and lines[k].strip().startswith("#"):
                comment_block.append(lines[k])
                k -= 1
            has_allow = bool(CURL_ALLOW_RE.search(line)) or any(
                CURL_ALLOW_RE.search(c) for c in comment_block
            )
            if not has_allow and not (
                CURL_MAXTIME_RE.search(joined) and CURL_CONNTIMEOUT_RE.search(joined)
            ):
                errors.append(
                    f"{path}:{i + 1}: curl missing --max-time/-m and/or --connect-timeout "
                    f"(a stalled request hangs the step up to the job timeout) - add both, "
                    f"or mark `# hygiene: allow-curl-no-timeout <reason>`"
                )
            i = j
        i += 1
    return errors


def lint_shell_script(path: Path, text: str) -> list[str]:
    """Rule 6. A standalone `.sh` must enable -e somewhere (inline `run:`
    blocks are NOT checked here - GitHub runs them under the default
    `bash -e {0}`, so -e is already applied there)."""
    if SET_E_ALLOW_RE.search(text):
        return []
    if SET_E_RE.search(text) or SHEBANG_E_RE.search(text):
        return []
    return [f"{path}: standalone shell script has no `set -e` (a failed command can "
            f"go unnoticed) - add it, or mark `# hygiene: allow-no-set-e <reason>`"]


def lint_job_timeouts(path: Path, text: str) -> list[str]:
    """Rule 7. Walk the jobs: map for missing timeout-minutes on any job
    that carries runs-on: (a real runner job, not a reusable-caller job)."""
    errors: list[str] = []
    lines = text.splitlines()
    in_jobs = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if JOBS_HEADER_RE.match(line):
            in_jobs = True
            i += 1
            continue
        if in_jobs:
            m = JOB_KEY_RE.match(line)
            if m:
                job_name = m.group(1)
                block_start = i + 1
                j = block_start
                while j < n and (lines[j].startswith("    ") or not lines[j].strip()):
                    j += 1
                block = "\n".join(lines[block_start:j])
                has_runs_on = bool(JOB_RUNS_ON_RE.search(block))
                has_uses = bool(JOB_USES_RE.search(block))
                has_timeout = bool(JOB_TIMEOUT_RE.search(block))
                has_allow = bool(TIMEOUT_ALLOW_RE.search(block))
                if has_runs_on and not has_uses and not has_timeout and not has_allow:
                    errors.append(
                        f"{path}:{i + 1}: job `{job_name}` has runs-on: but no "
                        f"timeout-minutes: (GitHub's 360-min default burns runner time "
                        f"silently on a hang) - add one, or mark "
                        f"`# hygiene: allow-no-timeout-minutes <reason>`"
                    )
                i = j
                continue
        i += 1
    return errors


def lint_file(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text()
    lines = text.splitlines()

    for n, line in enumerate(lines, 1):
        code = code_part(line)
        m = USES_RE.match(code)
        if m and not SHA_RE.match(m.group(2)):
            errors.append(
                f"{path}:{n}: unpinned action `{m.group(1)}@{m.group(2)}` - pin to a "
                f"full 40-char commit SHA on a Node-24-capable major (see #18)"
            )

    if path.suffix == ".sh":
        errors.extend(lint_shell_script(path, text))

    if path.suffix == ".yml":
        errors.extend(lint_curl_timeouts(path, text))
        if path.parent == WF_DIR:
            errors.extend(lint_job_timeouts(path, text))

    return errors


def main() -> int:
    targets: list[Path] = []
    if WF_DIR.is_dir():
        targets.extend(sorted(WF_DIR.glob("*.yml")))
    if GITHUB_DIR.is_dir():
        targets.extend(sorted(GITHUB_DIR.rglob("*.sh")))

    all_errors: list[str] = []
    for path in targets:
        all_errors.extend(lint_file(path))

    if not all_errors:
        print(f"workflow_hygiene: clean ({len(targets)} files checked)")
        return 0

    for err in all_errors:
        print(f"::error::{err}")
    print(f"workflow_hygiene: {len(all_errors)} violation(s) in {len(targets)} files checked")
    return 1


if __name__ == "__main__":
    sys.exit(main())
