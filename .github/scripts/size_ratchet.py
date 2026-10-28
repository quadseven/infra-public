#!/usr/bin/env python3
"""Check file line ceilings against trusted Git records, or only lower records.

Python stdlib and git only. Policy and records always come from --base, never
the working tree or proposed head. Exit 0: accepted; 1: violation; 2: cannot check.
See examples/size-ratchet/README.md for adoption and the JSON formats.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath


class CheckError(Exception):
    """The check cannot establish a verdict."""


def git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"git could not run: {exc}") from exc
    if result.returncode:
        raise CheckError(f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or ":" in value or path.is_absolute() or any(
        part.casefold() in ("", ".", "..", ".git") for part in value.rstrip("/").split("/")
    ):
        raise CheckError(f"not a repository-relative path: {value!r}")
    return value


def tree(repo: Path, ref: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for row in git(repo, "ls-tree", "-rz", "--full-tree", ref).split(b"\0"):
        if row:
            meta, name = row.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            if kind == "blob":
                entries[name.decode("utf-8")] = (mode, oid)
    return entries


def document(repo: Path, entries: dict[str, tuple[str, str]], path: str) -> dict[str, object]:
    if path not in entries:
        return {}
    mode, oid = entries[path]
    if mode not in ("100644", "100755"):
        raise CheckError(f"{path} must be a regular file")
    try:
        value = json.loads(git(repo, "cat-file", "blob", oid))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CheckError(f"{path} must contain a JSON object")
    return value


def positive(value: object) -> bool:
    return type(value) is int and value > 0


def policy_values(data: dict[str, object]) -> tuple[int, dict[str, int]]:
    if set(data) - {"default", "overrides"}:
        raise CheckError("policy permits only default and overrides")
    default, overrides = data.get("default", 500), data.get("overrides", {})
    if not positive(default) or not isinstance(overrides, dict):
        raise CheckError("default must be positive; overrides must be an object")
    for prefix, ceiling in overrides.items():
        safe_path(prefix)
        if not prefix.endswith("/") or not positive(ceiling):
            raise CheckError("overrides need folder/ keys and positive integer ceilings")
    return default, overrides


def ceiling(path: str, default: int, overrides: dict[str, int]) -> int:
    matches = [prefix for prefix in overrides if path.startswith(prefix)]
    return overrides[max(matches, key=len)] if matches else default


def evaluate(repo: Path, base: str, head: str, policy: str, records: str) -> tuple[list[str], dict[str, int]]:
    before, after = tree(repo, base), tree(repo, head)
    default, overrides = policy_values(document(repo, before, policy))
    limits = document(repo, before, records)
    for path, limit in limits.items():
        safe_path(path)
        if not positive(limit):
            raise CheckError("records require positive integer line counts")
    errors = []
    if before.get(records) != after.get(records):
        errors.append(f"{records}: record edits are reserved for the default-branch writer")
    lowered = {}
    for path, (mode, oid) in sorted(after.items()):
        if path in (policy, records) or mode not in ("100644", "100755"):
            continue
        content = git(repo, "cat-file", "blob", oid)
        lines = content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))
        floor = ceiling(path, default, overrides)
        limit = limits.get(path, floor)
        if lines > limit:
            errors.append(f"{path}: {lines} lines exceeds {'record' if path in limits else 'ceiling'} {limit}")
        if path in limits and lines > floor:
            lowered[path] = min(limit, lines)
    return errors, lowered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "lower"))
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--policy", default=".size-ratchet.json")
    parser.add_argument("--records", default=".size-ratchet-records.json")
    args = parser.parse_args(argv)
    try:
        policy, records = safe_path(args.policy), safe_path(args.records)
        if policy == records:
            raise CheckError("policy and records paths must differ")
        base, head = commit(args.repo, args.base), commit(args.repo, args.head)
        errors, lowered = evaluate(args.repo, base, head, policy, records)
        if errors:
            print("\n".join(errors))
            return 1
        if args.mode == "lower":
            if commit(args.repo, "HEAD") != head:
                raise CheckError("writer working tree is not at the requested head")
            if git(args.repo, "status", "--porcelain", "--untracked-files=all"):
                raise CheckError("writer requires a clean working tree")
            target = args.repo / records
            # No symlink in the output path may redirect a privileged writer.
            if any(parent.is_symlink() for parent in (target, *target.parents)):
                raise CheckError("writer records path must not traverse a symlink")
            old = document(args.repo, tree(args.repo, head), records)
            if lowered != old:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(lowered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"size-ratchet: {args.mode} accepted")
        return 0
    except (CheckError, UnicodeDecodeError, OSError) as exc:
        print(f"size-ratchet: cannot check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
