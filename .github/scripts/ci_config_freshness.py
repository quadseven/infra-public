#!/usr/bin/env python3
"""Fail a pull request whose branch is missing CI config changes from its base.

A pull request is checked by the workflows on its own branch, not by the ones
on the base branch. When the base gains a new or stricter gate after the PR
branched off - a workflow, a composite action, a ruleset, the validation
contract - the PR keeps running the old gates and can pass on rules the base
no longer accepts. "Require branches to be up to date" closes that gap, but it
forces a rebase for every unrelated base commit. This check is the narrow
version: it fails only when the base moved CI config after the PR's
merge-base (infra-public#168).

How: find the merge-base of HEAD and BASE, then list the commits on BASE's
first-parent chain after that merge-base that touch a watched path. Any such
commit is a gate this branch has not run. Each one is named (short sha,
subject, the watched files it touched) and the author is told to rebase onto
the base or merge it in.

Exit codes:
  0  the branch has every CI config change on the base
  1  the base has CI config commits this branch is missing (named)
  2  the check could not run: a ref that does not resolve, a shallow clone,
     no common history, a bad path list. Never a pass - a check that cannot
     see the history must not certify it.

Python stdlib only; needs git on PATH.

Run:
  python3 .github/scripts/ci_config_freshness.py --base origin/main
  python3 .github/scripts/ci_config_freshness.py --base origin/main \\
      --head HEAD --paths ".github/workflows/,ci/" --github
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field

# What counts as CI config when the caller names nothing. The single source
# of the default: the reusable workflow passes no list unless its caller does.
DEFAULT_PATHS: tuple[str, ...] = (
    ".github/workflows/",
    ".github/actions/",
    ".github/rulesets/",
    ".loom/validation.conf",
)

OK, STALE, CANNOT_CHECK = 0, 1, 2

# Separators git never puts in a sha or a one-line subject.
REC, UNIT = "\x1e", "\x1f"


class CheckError(Exception):
    """The check could not run. Maps to exit 2."""


@dataclass
class Commit:
    sha: str
    subject: str
    files: list[str] = field(default_factory=list)


@dataclass
class Result:
    base: str
    head: str
    merge_base: str
    paths: tuple[str, ...]
    missing: list[Commit]


def parse_paths(text: str) -> tuple[str, ...]:
    """Split a comma- or newline-separated path list. Blank entries and
    `#` comments are dropped. An entry starting with `-` is refused: it is
    passed after `--`, so it cannot become an option, but it is never a path
    anyone meant either."""
    paths: list[str] = []
    for line in text.replace(",", "\n").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if entry.startswith("-"):
            raise CheckError(
                f"path {entry!r} starts with '-'; name a path relative to the repo root"
            )
        if entry not in paths:
            paths.append(entry)
    if not paths:
        raise CheckError("the path list is empty; omit --paths to use the defaults")
    return tuple(paths)


def git(repo: str, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise CheckError(
            f"git {' '.join(args)} failed: {done.stderr.strip() or done.returncode}"
        )
    return done.stdout


def resolve(repo: str, ref: str, what: str) -> str:
    try:
        return git(
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{commit}}",
        ).strip()
    except CheckError:
        raise CheckError(
            f"{what} ref {ref!r} does not resolve to a commit in {repo!r}"
        ) from None


def check(repo: str, base: str, head: str, paths: tuple[str, ...]) -> Result:
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise CheckError(
            "the clone is shallow, so the merge-base cannot be trusted. Check out with full history (actions/checkout fetch-depth: 0)."
        )
    base_sha = resolve(repo, base, "base")
    head_sha = resolve(repo, head, "head")
    try:
        merge_base = git(repo, "merge-base", base_sha, head_sha).strip()
    except CheckError:
        raise CheckError(
            f"{base!r} and {head!r} share no history, so there is no merge-base to compare from"
        ) from None
    # --first-parent: walk the base branch's own history, so a merge commit
    # that brought CI changes in is named once, as the base sees it, and -m
    # makes that merge list the files it changed against its first parent.
    out = git(
        repo,
        "log",
        "--first-parent",
        "-m",
        "--name-only",
        f"--format={REC}%H{UNIT}%s",
        f"{merge_base}..{base_sha}",
        "--",
        *paths,
    )
    missing: list[Commit] = []
    for record in out.split(REC)[1:]:
        header, _, rest = record.partition("\n")
        sha, _, subject = header.partition(UNIT)
        files = [f for f in rest.splitlines() if f.strip()]
        missing.append(Commit(sha=sha, subject=subject, files=files))
    return Result(
        base=base, head=head, merge_base=merge_base, paths=paths, missing=missing
    )


def report(result: Result) -> str:
    if not result.missing:
        return (
            f"OK: {result.head} has every change to CI config on {result.base} "
            f"(merge-base {result.merge_base[:12]}; watched: {', '.join(result.paths)})."
        )
    n = len(result.missing)
    lines = [
        f"STALE: {result.base} has {n} commit{'s' if n != 1 else ''} changing CI config "
        f"since this branch forked from it at {result.merge_base[:12]}.",
        "This branch still runs the old gates, so a green result here does not mean the base's gates pass.",
        "",
    ]
    for c in result.missing:
        lines.append(f"  {c.sha[:12]}  {c.subject}")
        for f in c.files:
            lines.append(f"      {f}")
    lines += [
        "",
        f"Fix: bring {result.base} into this branch, then push.",
        f"  git fetch origin && git rebase {result.base}    (or: git merge {result.base})",
        f"Watched paths: {', '.join(result.paths)}",
    ]
    return "\n".join(lines)


def escape_annotation(text: str) -> str:
    """Escape a workflow-command message so a subject cannot end the
    annotation early or inject a second command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(result: Result) -> str:
    shas = ", ".join(f"{c.sha[:12]} ({c.subject})" for c in result.missing)
    msg = (
        f"{result.base} changed CI config after this branch forked at {result.merge_base[:12]}: {shas}. "
        f"Rebase onto {result.base} or merge it in, then push, so this PR runs the current gates."
    )
    return f"::error title=CI config is stale::{escape_annotation(msg)}"


def summary(result: Result) -> str:
    if not result.missing:
        return f"### CI config is current\n\nNo commit on `{result.base}` after `{result.merge_base[:12]}` touches a watched path.\n"
    rows = "\n".join(
        f"| `{c.sha[:12]}` | {c.subject.replace('|', '/')} | {', '.join(f'`{f}`' for f in c.files)} |"
        for c in result.missing
    )
    return (
        "### CI config is stale\n\n"
        f"`{result.base}` changed CI config after this branch forked at `{result.merge_base[:12]}`. "
        f"Rebase onto `{result.base}` or merge it in, then push.\n\n"
        "| Commit | Subject | Watched files |\n| --- | --- | --- |\n"
        f"{rows}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--base", required=True, help="the base branch ref, e.g. origin/main"
    )
    parser.add_argument("--head", default="HEAD", help="the PR head ref (default HEAD)")
    parser.add_argument(
        "--repo", default=".", help="the repository to check (default .)"
    )
    parser.add_argument(
        "--paths",
        default=None,
        help=f"comma- or newline-separated paths that count as CI config (default: {','.join(DEFAULT_PATHS)})",
    )
    parser.add_argument(
        "--github",
        action="store_true",
        help="also emit an ::error annotation and write $GITHUB_STEP_SUMMARY",
    )
    args = parser.parse_args(argv)
    try:
        paths = DEFAULT_PATHS if args.paths is None else parse_paths(args.paths)
        result = check(args.repo, args.base, args.head, paths)
    except CheckError as e:
        print(f"CANNOT CHECK: {e}", file=sys.stderr)
        if args.github:
            print(
                f"::error title=CI config freshness could not run::{escape_annotation(str(e))}"
            )
        return CANNOT_CHECK
    print(report(result))
    if args.github:
        if result.missing:
            print(annotation(result))
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(summary(result))
    return STALE if result.missing else OK


if __name__ == "__main__":
    sys.exit(main())
