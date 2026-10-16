#!/usr/bin/env python3
"""Validate .loom/validation.conf against the workflows it claims to come from
(infra-public#120).

The contract is what a lane reads to learn which commands this repository
runs before a pull request opens. A drifted contract is worse than no
contract: it sends the lane through commands nobody runs, while the check it
was supposed to prepare for is never the one that was run. So every command
is verified twice - it must be a run step of a pull-request workflow in this
tree, and with --run it must exit 0 here, now.

Adding a command to the contract makes CI execute it (see
.github/workflows/check.loom-validation.yml), exactly as adding it to a
workflow would. Both are review-visible changes; neither is a free note.

`--run` executes a command directly - split into arguments and handed to
execve, never to a shell. The contract is a file in the repository, and
handing its text to a shell would turn it into a script: repository text run
as a script, in a job that holds a token. So a command carrying shell syntax
(`&&`, a redirect, a substitution) is refused outright, and the way to record
a step that runs several commands is one contract line per command.

Python stdlib only - the workflow parsing is regex over the YAML text, not a
YAML load, for the same reason workflow_hygiene.py does it that way: these
guard scripts run in CI jobs that install nothing.

Run: python3 .github/scripts/validation_contract.py [--run]

Exit 0 = contract valid (and, with --run, every command passed);
exit 1 = violations, printed as ::error:: for CI annotation.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = Path(".loom/validation.conf")

# The order a contract MUST list its kinds in: what a lane runs before it
# opens a pull request, cheapest and broadest first. A kind with no command
# in this repository is simply absent from the file.
KINDS = ("lint", "format", "type", "test")

# Fields per line: kind | command | workflow file | step name.
FIELD_COUNT = 4

# A command that hangs must not hang CI with it.
DEFAULT_COMMAND_TIMEOUT = 900

# A `run:` block scalar opener (`|`, `|-`, `>`, `>-`, with an optional
# explicit indentation indicator) versus a one-line `run: <script>`.
BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*$")
LIST_ITEM_RE = re.compile(r"^(\s*)-\s+(.*)$")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):\s*(.*)$")
# The top-level trigger block. `on:` unindented, so a job key or a step key
# of the same name cannot be mistaken for it.
ON_HEADER_RE = re.compile(r"^on:\s*(.*)$")
PR_TRIGGER_RE = re.compile(r"^\s{2}pull_request:\s*")
# Shell syntax a contract command may not carry. A contract command is one
# command, run directly; anything here is a shell script, and running one
# means handing the text of a repository file to a shell (ruff S602).
SHELL_OPERATOR_RE = re.compile(r"&&|\|\||[|;&<>`]|\$\(")
OUTPUT_TAIL_LINES = 5
OUTPUT_TAIL_CHARS = 500


class ContractEntry(NamedTuple):
    """One command in the contract, with the workflow step it comes from."""

    lineno: int
    kind: str
    command: str
    workflow: str
    step: str


class WorkflowStep(NamedTuple):
    """A named `run:` step, and the working directory it runs in."""

    name: str
    run: str
    working_directory: str


def shell_operator(command: str) -> str | None:
    """The first piece of shell syntax in `command`, or None if it has none.

    A contract command is executed directly, never by a shell, so `&&`, a
    pipe or a substitution in one is either a command that cannot be run or a
    command that would not run what the step runs. Either way the lane is
    better served by giving each of the step's commands its own line.
    """
    found = SHELL_OPERATOR_RE.search(command)
    return found.group(0) if found else None


def parse_contract(text: str) -> tuple[list[ContractEntry], list[str]]:
    """Parse contract text into entries and the errors found while parsing.

    Blank lines and whole-line `#` comments are ignored; everything else is
    `kind | command | workflow file | step name`.
    """
    entries: list[ContractEntry] = []
    errors: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != FIELD_COUNT:
            errors.append(
                f"{lineno}: expected {FIELD_COUNT} fields "
                f"(`kind | command | workflow | step`), got {len(fields)}: {line}"
            )
            continue
        if any(not field for field in fields):
            errors.append(f"{lineno}: empty field: {line}")
            continue
        kind = fields[0]
        if kind not in KINDS:
            errors.append(
                f"{lineno}: unknown kind {kind!r} (expected one of {', '.join(KINDS)}): {line}"
            )
            continue
        operator = shell_operator(fields[1])
        if operator is not None:
            errors.append(
                f"{lineno}: {operator!r} is shell syntax, and a contract command is one "
                f"command run directly - give each command of that step its own line: {line}"
            )
            continue
        entries.append(ContractEntry(lineno, kind, fields[1], fields[2], fields[3]))
    if not entries and not errors:
        errors.append(
            "no commands: a contract with no commands tells a lane to run nothing"
        )
    return entries, errors


def check_order(entries: list[ContractEntry]) -> list[str]:
    """Enforce KINDS order: a kind may repeat, but never after a later kind."""
    errors: list[str] = []
    highest = -1
    for entry in entries:
        index = KINDS.index(entry.kind)
        if index < highest:
            errors.append(
                f"{entry.workflow}:{entry.lineno}: `{entry.kind}` is listed after "
                f"`{KINDS[highest]}`; the contract lists {', '.join(KINDS)} in that order"
            )
            continue
        highest = index
    return errors


def parse_steps(text: str) -> list[WorkflowStep]:
    """Every named `run:` step in a workflow, in file order.

    Step keys sit two columns right of their `- ` (a `with:` or `env:` key
    sits four), which is what keeps a nested `name:` input from being read as
    a step name. Unnamed steps are dropped: a contract names its step, so a
    step with no name cannot be referenced.
    """
    steps: list[WorkflowStep] = []
    lines = text.splitlines()
    step_indent: int | None = None
    name = working_directory = ""
    index = 0
    while index < len(lines):
        rest = _step_item_text(lines[index], step_indent)
        if rest is None:
            index += 1
            continue
        item = LIST_ITEM_RE.match(lines[index])
        if item:
            step_indent = len(item.group(1))
        pair = KEY_RE.match(rest)
        key, value = (pair.group(1), pair.group(2)) if pair else ("", "")
        index += 1
        if key == "name":
            name = _scalar(value)
            working_directory = ""
        elif key == "working-directory":
            working_directory = _scalar(value)
        elif key == "run":
            if BLOCK_SCALAR_RE.match(value.strip()):
                script, index = _block_scalar(lines, index - 1, step_indent)
            else:
                script = value.strip()
            steps.append(WorkflowStep(name, script, working_directory))
        else:
            # Any other step key (`uses:`, `with:`, `env:`, `if:` ...) belongs
            # to a step of its own; only `name:`, `run:` and
            # `working-directory:` describe the step the contract can name.
            name = ""
            working_directory = ""
    return [step for step in steps if step.name]


def _step_item_text(line: str, step_indent: int | None) -> str | None:
    """The `key: value` text of a step's own key, or None for anything else.

    A `- ` list item starts a step at its own indent. A bare key belongs to
    the step in progress only at exactly two columns in from that indent -
    deeper lines are a nested mapping (`with:`, `env:`).
    """
    item = LIST_ITEM_RE.match(line)
    if item:
        return item.group(2)
    if step_indent is None or not line.strip():
        return None
    indent = len(line) - len(line.lstrip())
    if indent != step_indent + 2:
        return None
    return line.strip()


def _block_scalar(
    lines: list[str], index: int, step_indent: int | None
) -> tuple[str, int]:
    """Collect the lines of a block scalar opened at `index`, and the index
    of its first line after the block."""
    body: list[str] = []
    index += 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and (len(line) - len(line.lstrip())) <= (step_indent or 0):
            break
        body.append(line)
        index += 1
    return "\n".join(body), index


def _scalar(value: str) -> str:
    """A YAML scalar as written: quotes stripped, block markers emptied."""
    text = value.strip()
    if BLOCK_SCALAR_RE.match(text):
        return ""
    return text.strip("\"'")


def is_pull_request_workflow(text: str) -> bool:
    """True when the workflow's `on:` block carries a `pull_request:` trigger.

    `pull_request_target` is a different trigger and does not count: the
    contract may only claim checks a pull request actually runs.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        header = ON_HEADER_RE.match(line)
        if not header:
            continue
        if "pull_request" in _trigger_list(header.group(1)):
            return True
        if _block_declares_pull_request(lines, index + 1):
            return True
    return False


def _trigger_list(value: str) -> set[str]:
    """The triggers named inline (`on: [pull_request]`, `on: pull_request`)."""
    cleaned = value.strip().strip("[]{}").replace('"', "").replace("'", "")
    return {part.strip() for part in cleaned.split(",") if part.strip()}


def _block_declares_pull_request(lines: list[str], start: int) -> bool:
    for line in lines[start:]:
        if line.strip() and not line[:1].isspace():
            return False
        if PR_TRIGGER_RE.match(line):
            return True
    return False


def command_matches(command: str, run_script: str) -> bool:
    """Whether a step's script runs `command`.

    Whitespace is collapsed on both sides so a command written on one line
    matches a step that spells it across a continuation, and vice versa.
    """
    return " ".join(command.split()) in " ".join(run_script.split())


def check_repository(entries: list[ContractEntry], root: Path = REPO_ROOT) -> list[str]:
    """Every error in the contract: ordering, then each entry against the
    workflow file and step it names."""
    errors = check_order(entries)
    for entry in entries:
        errors.extend(_entry_errors(entry, root))
    return errors


def _entry_errors(entry: ContractEntry, root: Path) -> list[str]:
    where = f"{entry.workflow}:{entry.lineno}"
    path = root / entry.workflow
    if not path.is_file():
        return [f"{where}: no such workflow: {entry.workflow}"]
    text = path.read_text()
    if not is_pull_request_workflow(text):
        return [f"{where}: {entry.workflow} is not triggered by pull_request"]
    steps = [step for step in parse_steps(text) if step.name == entry.step]
    if not steps:
        return [f"{where}: no step named {entry.step!r} in {entry.workflow}"]
    relocated = [step for step in steps if step.working_directory]
    if len(relocated) == len(steps):
        return [
            f"{where}: step {entry.step!r} runs in working-directory "
            f"{relocated[0].working_directory!r}, which this format does not carry - "
            f"extend the format rather than record a command that would run "
            f"from the wrong directory"
        ]
    for step in steps:
        if command_matches(entry.command, step.run):
            return []
    return [
        f"{where}: {entry.workflow} step {entry.step!r} does not run {entry.command!r}"
    ]


def run_commands(
    entries: list[ContractEntry],
    root: Path = REPO_ROOT,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> list[str]:
    """Run each command in contract order, from `root`. The list of errors
    is what a lane needs: which command went red, and what it printed."""
    errors: list[str] = []
    for entry in entries:
        errors.extend(_run_one(entry, root, timeout))
    return errors


def _run_one(entry: ContractEntry, root: Path, timeout: int) -> list[str]:
    where = f"{entry.workflow}:{entry.lineno}"
    # The parser refuses a shell script, but run_commands is callable on its
    # own, so the refusal is repeated here rather than assumed.
    operator = shell_operator(entry.command)
    if operator is not None:
        return [
            f"{where}: refusing to run {entry.command!r}: {operator!r} is shell syntax, "
            f"and a contract command is one command run directly - give each command "
            f"of that step its own line"
        ]
    try:
        argv = shlex.split(entry.command)
    except ValueError as error:
        return [f"{where}: cannot split {entry.command!r} into a command: {error}"]
    try:
        # S603 (execution of input from outside the program) is the cost of not
        # using shell=True, and this is where the contract is meant to spend it:
        # no shell parses the text, so `&&` cannot smuggle a second command in,
        # and the operator guard above has already refused any line carrying it.
        done = subprocess.run(  # noqa: S603
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [f"{where}: `{entry.command}` did not finish within {timeout}s"]
    except OSError as error:
        return [f"{where}: no such command: {entry.command}: {error}"]
    if done.returncode == 0:
        return []
    return [
        f"{where}: `{entry.command}` failed with exit "
        f"{done.returncode}: {_output_tail(done)}"
    ]


def _output_tail(done: subprocess.CompletedProcess[str]) -> str:
    """The last lines of the command's output, on one line - the part that
    says why it went red."""
    combined = "\n".join(part for part in (done.stdout, done.stderr) if part)
    lines = [line for line in combined.splitlines() if line.strip()]
    return " | ".join(lines[-OUTPUT_TAIL_LINES:])[:OUTPUT_TAIL_CHARS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate .loom/validation.conf.")
    parser.add_argument(
        "--contract", type=Path, default=DEFAULT_CONTRACT, help="contract file to check"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository root the paths resolve from",
    )
    parser.add_argument(
        "--run",
        dest="run",
        action="store_true",
        help="also run every command in the contract",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_COMMAND_TIMEOUT,
        help="per-command timeout, seconds",
    )
    args = parser.parse_args(argv)

    if not args.contract.is_file():
        print(f"::error::{args.contract}: no validation contract to check")
        return 1

    entries, errors = parse_contract(args.contract.read_text())
    if not errors:
        errors = check_repository(entries, args.root)
    if args.run and not errors:
        errors = run_commands(entries, args.root, args.timeout)

    if errors:
        for error in errors:
            print(f"::error::{error}")
        print(f"validation_contract: {len(errors)} violation(s) in {args.contract}")
        return 1
    ran = " (and ran clean)" if args.run else ""
    print(
        f"validation_contract: {args.contract} - {len(entries)} command(s), clean{ran}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
