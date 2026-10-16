#!/usr/bin/env python3
"""Workflow hygiene checks for this public reusable workflow library.

Ported canonical rules:
  Rule 1: pin third-party actions to full commit SHAs.
  Rule 5: bound curl calls with total and connection timeouts.
  Rule 6: enable `set -e` in standalone shell scripts.
  Rule 7: set `timeout-minutes` for runner jobs.
  Rule 8: restore the working tree before using a local action after a
          branch switch.
  Rule 9: pass attacker-controlled expressions through `env`, not `run` text.
  Rule 11: use one version label for each pinned action SHA, repo-wide.
  Rule 12: avoid `gh` calls in jobs on an `arc-*` runner label.

The local Rule 1b also rejects pinned actions labelled with an EOL Node
major. Rules 3, 4, and 10 are declared skips: this repository has no
environment-secret manifest, ARC runner fleet, or Pulumi stack tree.
Rules 2 and 13 concern infrastructure absent from this repository.

Fixtures under .github/scripts/fixtures/ record canonical verdicts for
rules 5, 8, 11, and 12. Set CANONICAL_HYGIENE_LINTER to a local copy of
the canonical script to recheck those recorded verdicts.

Run: python3 .github/scripts/workflow_hygiene.py
Exit 0 means clean; exit 1 reports violations as CI annotations.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

GITHUB_DIR = Path(".github")
WF_DIR = GITHUB_DIR / "workflows"
FIXTURES_DIR = GITHUB_DIR / "scripts" / "fixtures"
ACTIONS_DIR = GITHUB_DIR / "actions"

# Rules in scope of the canonical set that this repo skips, with why. The run
# summary names them so a clean run cannot be read as having checked them.
SKIPPED_RULES: dict[int, str] = {
    # Rule 3 - env-scoped secrets: no list of them, no manifest to derive one.
    3: "no environment-scoped secret list",
    # Rule 4 - ARC runner routing: no ARC fleet, plain GitHub-hosted runners.
    4: "no ARC runner fleet",
    # Rule 10 - PR-preview reachability: no iac.pulumi.* stacks.
    10: "no Pulumi stacks",
}

# Local composites (./.github/actions/...) and same-repo reusables have no
# pinning concern; only owner/repo@ref refs are checked. `['"]?` before the
# owner tolerates a quoted YAML scalar (`uses: "actions/checkout@v4"`) -
# the ref capture group's own charset already excludes the closing quote,
# so no corresponding change is needed on the right side.
USES_RE = re.compile(
    r"^\s*-?\s*uses:\s*['\"]?([A-Za-z0-9_-]+/[A-Za-z0-9._/-]+)@([A-Za-z0-9._-]+)"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Rule 1b - EOL Node major detection. A SHA-pinned action that carries a
# comment indicating a known-EOL major (e.g. `# v4` on actions/checkout or
# actions/setup-node) passes the SHA check but still runs on EOL Node.
# The comment is written at whatever precision the author felt like: `# v4`,
# `# v4.2` and `# v4.2.2` all name the same Node-20 major. Capture the MAJOR
# alone and let the rest of the version float, so precision cannot decide the
# verdict - an earlier form captured `v\d+(?:\.\d+)?` and compared THAT against
# the bare-major table, so `# v4.2.2` reduced to `v4.2`, missed `{"v4"}` and
# silently evaded the rule (#72; same failure mode as #64, which Rule 1b exists
# to catch).
PIN_COMMENT_RE = re.compile(r"#\s*(v\d+)(?:\.\d+)*")
# Actions known to have EOL majors. `actions/checkout` v4 and
# `actions/setup-node` v4 both run on Node-20 (EOL). v5+ runs on Node-24.
EOL_MAJOR: dict[str, set[str]] = {
    "actions/checkout": {"v4"},
    "actions/setup-node": {"v4"},
}
# All majors in EOL_MAJOR that are below the Node-24-capable floor.
# (v5+ = Node-24 capable for these actions.)

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


# A `description:` block scalar (`|` or `>`) is prose: reusable and composite
# inputs often quote an example command (`... | curl -fsS ...`) that never runs.
DESC_BLOCK_RE = re.compile(r"^(\s*)description:\s*[|>]")
# A single-line `description:` or `name:` value is prose in either quoting
# style: `description: "Per-request curl --max-time in seconds."` documents the
# input that SUPPLIES the bound. Deliberately NOT "only lines inside run:
# scripts": callers pass real curl commands through other fields (a
# `smoke-cmd: curl ...` input to a reusable that executes it), and those must
# stay checked.
PROSE_FIELD_RE = re.compile(r"^\s*(?:-\s*)?(?:description|name):\s*(?![|>])\S")


def _description_block_lines(lines: list[str]) -> set[int]:
    """0-based indices of the lines inside a `description:` block scalar."""
    inside: set[int] = set()
    i = 0
    n = len(lines)
    while i < n:
        m = DESC_BLOCK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        i += 1
        while i < n and (
            not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent
        ):
            inside.add(i)
            i += 1
    return inside


def _comment_run_above(lines: list[str], start: int) -> list[str]:
    """The contiguous comment-only lines directly above line `start`. An
    opt-out whose reason wraps onto more comment lines still counts; a blank
    or code line ends the run."""
    run: list[str] = []
    k = start - 1
    while k >= 0 and lines[k].strip().startswith("#"):
        run.append(lines[k])
        k -= 1
    return run


def lint_curl_timeouts(path: Path, text: str) -> list[str]:
    """Rule 5. For each real `curl` invocation, in any field of a workflow or
    anywhere in a `.sh`, join backslash-continued lines into one logical
    command and require both a total-time bound and a connect bound, read
    from code only (a bound in a trailing comment does not count). Skips a
    curl that appears only in a comment, inside a `description:` block
    scalar, or in a single-line `description:`/`name:` value, and one opted
    out by the allow-marker on any line of the command or anywhere in the
    comment lines directly above it."""
    errors: list[str] = []
    lines = text.splitlines()
    desc_lines = _description_block_lines(lines)
    n = len(lines)
    i = 0
    while i < n:
        code = code_part(lines[i])
        if (
            i in desc_lines
            or PROSE_FIELD_RE.match(lines[i])
            or not CURL_RE.search(code)
        ):
            i += 1
            continue
        start = i
        joined = code
        span = [lines[i]]
        while code_part(lines[i]).rstrip().endswith("\\") and i + 1 < n:
            i += 1
            joined += " " + code_part(lines[i])
            span.append(lines[i])
        opted_out = any(
            CURL_ALLOW_RE.search(ln) for ln in span + _comment_run_above(lines, start)
        )
        if not opted_out and not (
            CURL_MAXTIME_RE.search(joined) and CURL_CONNTIMEOUT_RE.search(joined)
        ):
            errors.append(
                f"{path}:{start + 1}: curl missing --max-time/-m and/or --connect-timeout "
                f"(a stalled request hangs the step up to the job timeout) - add both, "
                f"or mark `# hygiene: allow-curl-no-timeout <reason>`"
            )
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
    return [
        f"{path}: standalone shell script has no `set -e` (a failed command can "
        f"go unnoticed) - add it, or mark `# hygiene: allow-no-set-e <reason>`"
    ]


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


# Rule 9 - GHA template injection inside `run:` blocks. Ported verbatim from
# the canonical linter. GitHub expands
# `${{ }}` into the script TEXT before the shell parses it, so a value
# containing a quote can close it and run commands.
#
# SCOPED ON PURPOSE to the two attacker-influenceable families. Flagging every
# `${{ }}` would also hit `secrets.*`, `inputs.*`, `matrix.*` and `github.sha`,
# which are common and mostly benign - a rule that noisy gets suppressed rather
# than obeyed, and then catches nothing.
#   - steps.*.outputs.*  : upstream release tags, API bodies, scraped RSS
#   - github.event.*     : PR titles, branch names, issue bodies
# The fix is always the same: pass it via `env:` and use "$VAR" in the script.
TEMPLATE_INJECTION_RE = re.compile(
    r"\$\{\{\s*(steps\.[\w-]+\.outputs\.[\w-]+|github\.event\.[\w.]+)\s*\}\}"
)
# A `run:` block scalar opener: `run: |`, `run: |-`, `- run: >`, etc. A one-line
# `run: echo hi` is still script text, so it is matched too.
RUN_BLOCK_RE = re.compile(r"^\s*(?:-\s*)?run:\s*(\|-?|>-?)?\s*$")
RUN_INLINE_RE = re.compile(r"^\s*(?:-\s*)?run:\s+(?!\|)(?!>)\S")
TEMPLATE_INJECTION_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-interpolation\b")


def _run_script_lines(lines: list[str]):
    """Yield (lineno, line) for every line that is SHELL SCRIPT TEXT.

    Block extent is tracked by indentation: a `run: |` opens the block and the
    first non-blank line at or left of the `run:` key's own indent closes it.
    That boundary keeps a FOLLOWING step's `env:` - which legitimately carries
    these expressions, and is the fix - from being read as script.
    """
    in_run = False
    run_indent = 0
    for n, line in enumerate(lines, 1):
        if RUN_BLOCK_RE.match(line):
            in_run = True
            run_indent = len(line) - len(line.lstrip())
            continue
        if RUN_INLINE_RE.match(line):
            in_run = False
            yield n, line  # single-line `run: echo ...` is script too
            continue
        if not in_run:
            continue
        if line.strip() and (len(line) - len(line.lstrip())) <= run_indent:
            in_run = False
            continue
        yield n, line


def lint_template_injection(path: Path, text: str) -> list[str]:
    """Rule 9 - no attacker-influenceable `${{ }}` inside a `run:` script."""
    return [
        _injection_error(path, n, m.group(1))
        for n, line in _run_script_lines(text.splitlines())
        if not TEMPLATE_INJECTION_ALLOW_RE.search(line)
        for m in TEMPLATE_INJECTION_RE.finditer(line)
    ]


def _injection_error(path: Path, n: int, expr: str) -> str:
    return (
        f"{path}:{n}: `${{{{ {expr} }}}}` interpolated inside a `run:` script - "
        f"GitHub pastes it into the script TEXT, so a crafted value can close "
        f"the quote and execute. Pass it via `env:` and reference "
        f'"$VAR" instead'
    )


# Rule 8 - working-tree branch switch before a local action (#1850). A
# path-scoped `git checkout <ref> -- <path>` only copies files out and is
# FINE; a bare `git checkout <branch>` / `git switch <branch>` replaces the
# whole tree. Returning via an explicit sha, `-`, or `@{-1}` restores it.
BRANCH_SWITCH_RE = re.compile(
    r"\bgit\s+(?:checkout|switch)\s+(?:-b\s+|-B\s+|-c\s+)?"
    r"(?P<ref>[A-Za-z0-9._/${}\"\'-]+)"
)
LOCAL_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\./[^\s]+)")
BRANCH_SWITCH_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-branch-switch\b")


def _switch_line_after(code: str, n: int, switch_line: int | None) -> int | None:
    """The line of the branch switch still in force after `code` (line `n`):
    a real switch sets it, a return to the original ref clears it, a
    path-scoped checkout leaves it alone."""
    m = BRANCH_SWITCH_RE.search(code)
    if not m:
        return switch_line
    if "--" in code[m.end() :]:
        return switch_line
    ref = m.group("ref")
    # `-`, `@{-1}`, or an interpolated sha/var is a RETURN, not a switch
    if ref in ("-", "@{-1}") or "$" in ref:
        return None
    return n


def lint_branch_switch_local_action(path: Path, text: str) -> list[str]:
    """Rule 8. Find each working-tree branch switch, then look for a later
    local `uses: ./...` with no return-to-ref in between. Reports the `uses:`
    line, since that is where CI actually dies."""
    if BRANCH_SWITCH_ALLOW_RE.search(text):
        return []
    errors: list[str] = []
    switch_line: int | None = None
    for n, line in enumerate(text.splitlines(), 1):
        code = code_part(line)
        if BRANCH_SWITCH_RE.search(code):
            switch_line = _switch_line_after(code, n, switch_line)
            continue
        u = LOCAL_USES_RE.match(code)
        if u and switch_line is not None:
            errors.append(
                f"{path}:{n}: Rule 8: local action `{u.group(1)}` is used after the "
                f"working tree was switched to another branch at line "
                f"{switch_line} - that branch does not carry `.github/`, so "
                f"the action is gone and this step fails with \"Can't find "
                f"'action.yml'\". Capture ORIG_SHA=$(git rev-parse HEAD) "
                f'before the switch and `git checkout "$ORIG_SHA"` after '
                f"committing (#1850)"
            )
    return errors


# Rule 12 - no `gh` CLI in a job on the arc pool (#3896). A real call is `gh`
# followed by a subcommand, at the start of a command: `ghcr.io`, `ghost` and
# `gh` inside a comment are not calls.
GH_CALL_RE = re.compile(
    r"(?:^|[\s;&|(`\"'])gh\s+(?:api|issue|pr|run|release|repo|workflow|label|search|"
    r"secret|variable|auth|gist|cache|project|attestation|extension)\b"
)
GH_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-gh\b")
# An arc label, not a label that merely contains `arc-` (srv-arc-foo).
ARC_IN_RUNS_ON_RE = re.compile(r"(?<![\w-])arc-[a-z0-9-]+")


def _shell_code(line: str) -> str:
    """The line up to its shell comment.

    As in the shell itself, a comment starts at an UNQUOTED `#` that begins a
    word. `${REF#refs/heads/}`, `$#`, a URL fragment and a `#` inside quotes
    (`echo "a # b" && gh ...`) are all code, and cutting at any of them would
    hide a `gh` call later on the same line. A regex cannot track quotes, so
    this walks the line. Also applied to the `runs-on:` line, where YAML's
    comment rule is the same.
    """
    quote = None
    prev = " "
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'":
            i += 2  # an escaped character is never a quote or a comment
            prev = "x"
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and prev.isspace():
            return line[:i]
        prev = ch
        i += 1
    return line


def _job_blocks(lines: list[str]):
    """Yield (job_name, key_index, block) for each job in the `jobs:` map,
    where block is the key line plus everything indented deeper than it (>2
    spaces) up to the next job key or a dedent to top level."""
    n = len(lines)
    i = 0
    in_jobs = False
    while i < n:
        line = lines[i]
        if JOBS_HEADER_RE.match(line):
            in_jobs = True
        elif in_jobs and line.strip() and not line[0].isspace():
            # A non-indented, non-blank line ends the jobs: map (a comment
            # at column 0 is not a key and does not).
            in_jobs = line.lstrip().startswith("#")
        m = JOB_KEY_RE.match(line) if in_jobs else None
        if not m:
            i += 1
            continue
        j = i + 1
        while j < n and not (lines[j].strip() and _indent(lines[j]) <= 2):
            j += 1
        yield m.group(1), i, lines[i:j]
        i = j


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def lint_gh_on_arc(path: Path, text: str) -> list[str]:
    """Rule 12. For each job whose `runs-on:` names an `arc-*` label, flag
    every `run:` script line that calls `gh`."""
    errors: list[str] = []
    for job_name, key_line, block in _job_blocks(text.splitlines()):
        runs_on = next((b for b in block if JOB_RUNS_ON_RE.match(b)), None)
        label = ARC_IN_RUNS_ON_RE.search(_shell_code(runs_on)) if runs_on else None
        if label is None or any(GH_ALLOW_RE.search(b) for b in block):
            continue
        for n, line in _run_script_lines(block):
            if GH_CALL_RE.search(_shell_code(line)):
                errors.append(
                    f"{path}:{key_line + n}: Rule 12: `gh` in job `{job_name}` on "
                    f"`{label.group(0)}` - the arc pool has no gh CLI, so this "
                    f"dies with exit 127 at run time and nothing before then "
                    f"can see it. Use curl + jq, or an inline "
                    f"`# hygiene: allow-gh <reason>` if the job installs gh"
                )
    return errors


# Rule 11 - one version label per pinned action (#2828). The trailing
# `# vN` is what a reader trusts; one SHA under two labels means one is
# wrong. PIN_LABEL_RE reads the raw line, because code_part() strips the
# very comment it needs.
PIN_LABEL_RE = re.compile(
    r"""^\s*-?\s*uses:\s*['"]?([A-Za-z0-9_-]+/[A-Za-z0-9._/-]+@[0-9a-f]{40})['"]?\s*#\s*(v[0-9][0-9A-Za-z.+-]*)"""
)
# Deliberately looser, and only on a `uses:` key: a 40-hex ref followed by a
# `# v...` comment. A line this matches that PIN_LABEL_RE does not is a pin the
# check cannot see, so it is reported by line rather than silently skipped.
PIN_LABEL_LOOSE_RE = re.compile(r"^\s*-?\s*\{?\s*uses:.*@[0-9a-f]{40}\S*\s*#\s*v[0-9]")


def _collect_pin_labels(
    path: Path, text: str, sites: dict[str, dict[str, list[str]]], unreadable: list[str]
) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        m = PIN_LABEL_RE.match(line)
        if m:
            sites.setdefault(m.group(1), {}).setdefault(m.group(2), []).append(
                f"{path}:{n}"
            )
        elif PIN_LABEL_LOOSE_RE.match(line):
            unreadable.append(f"{path}:{n}")


def lint_pin_labels(files: list[Path]) -> tuple[list[str], str]:
    """Rule 11 across every workflow and composite action, as (errors,
    summary). Compared repo-wide, since a conflict is files disagreeing.

    Both matchers start at a `uses:` key, so a commented-out step or a
    prose comment is never read as a pin. A labelled pin the strict matcher
    cannot read is an error naming its line, even when other pins were read,
    so a quoted or flow-style pin cannot hide a conflict."""
    sites: dict[str, dict[str, list[str]]] = {}
    unreadable: list[str] = []
    for path in files:
        _collect_pin_labels(path, path.read_text(), sites, unreadable)
    errors = [
        f"Rule 11: {pin}: one SHA under {len(labels)} version labels ("
        + "; ".join(
            f"`# {lab}` at {', '.join(where)}" for lab, where in sorted(labels.items())
        )
        + ") - at least one is wrong; label every site with the release this SHA is (#2828)"
        for pin, labels in sorted(sites.items())
        if len(labels) > 1
    ]
    if unreadable:
        errors.append(
            f"Rule 11: pin-label check: {len(unreadable)} labelled pin(s) the matcher cannot read, so a "
            f"conflict there would be missed: {', '.join(unreadable)} - write them as "
            f"`uses: owner/action@<sha>  # vN` (#2828)"
        )
    conflicting = sum(len(labels) > 1 for labels in sites.values())
    return errors, (
        f"workflow_hygiene: {len(sites)} distinct labelled action@sha pins, "
        f"{conflicting} with conflicting labels"
    )


def skipped_rules_label() -> str:
    """`rules 3, 4 and 10 skipped`, in the canonical linter's wording."""
    nums = [str(n) for n in sorted(SKIPPED_RULES)]
    named = nums[0] if len(nums) == 1 else f"{', '.join(nums[:-1])} and {nums[-1]}"
    return f"rule{'s' if len(nums) > 1 else ''} {named} skipped"


def lint_file(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text()
    lines = text.splitlines()

    for n, line in enumerate(lines, 1):
        code = code_part(line)
        m = USES_RE.match(code)
        if m:
            ref = m.group(2)
            if not SHA_RE.match(ref):
                errors.append(
                    f"{path}:{n}: unpinned action `{m.group(1)}@{ref}` - pin to a "
                    f"full 40-char commit SHA on a Node-24-capable major (see #18)"
                )
            else:
                # SHA-pinned - check for EOL Node major in the comment.
                action = m.group(1)
                if action in EOL_MAJOR:
                    comment_match = PIN_COMMENT_RE.search(line)
                    if comment_match and comment_match.group(1) in EOL_MAJOR[action]:
                        # Report the comment as WRITTEN, not the reduced major -
                        # `# v4.2.2` is what the author has to go find and edit.
                        written = comment_match.group(0).split("#", 1)[1].strip()
                        errors.append(
                            f"{path}:{n}: action `{action}@{ref}` is SHA-pinned but "
                            f"commented as `{written}` (major "
                            f"`{comment_match.group(1)}` = Node-20/EOL) - "
                            f"re-pin to a Node-24-capable major (v5+) at a full SHA"
                        )

    if path.suffix == ".sh":
        errors.extend(lint_shell_script(path, text))
        errors.extend(lint_curl_timeouts(path, text))

    if path.suffix == ".yml":
        errors.extend(lint_curl_timeouts(path, text))
        errors.extend(lint_template_injection(path, text))
        if path.parent == WF_DIR:
            errors.extend(lint_job_timeouts(path, text))
            errors.extend(lint_branch_switch_local_action(path, text))
            errors.extend(lint_gh_on_arc(path, text))

    return errors


def main() -> int:
    targets: list[Path] = []
    if WF_DIR.is_dir():
        targets.extend(sorted(WF_DIR.glob("*.yml")))
    if GITHUB_DIR.is_dir():
        # The parity corpora hold deliberately bad cases (some `.sh`): test
        # data, not scripts this repo runs.
        targets.extend(
            p for p in sorted(GITHUB_DIR.rglob("*.sh")) if FIXTURES_DIR not in p.parents
        )

    all_errors: list[str] = []
    for path in targets:
        all_errors.extend(lint_file(path))
    pin_errors, pin_summary = lint_pin_labels(
        sorted(WF_DIR.glob("*.yml")) + sorted(ACTIONS_DIR.glob("**/action.yml"))
    )
    all_errors.extend(pin_errors)
    print(pin_summary)

    where = f"{len(targets)} files checked; {skipped_rules_label()}"
    if not all_errors:
        print(f"workflow_hygiene: clean ({where})")
        return 0

    for err in all_errors:
        print(f"::error::{err}")
    print(f"workflow_hygiene: {len(all_errors)} violation(s) in {where}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
