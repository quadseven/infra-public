#!/usr/bin/env python3
"""Block private-infrastructure identifiers from reaching a PUBLIC repo.

WHY THIS EXISTS
---------------
templates/public-repo/AGENTS.md is the written rule: nothing identifying a
specific person, a specific private network, or a specific credential may
appear anywhere in a public repo, on any surface. A policy file is not
enforcement, and the evidence said enforcement was what was missing - every
public repo in the fleet was checked and none had a per-PR leak check, so each
fix had been a one-time manual scrub sitting next to an unscrubbed sibling.

None of what leaks here is secret-SHAPED. No key, no token, no password - so a
secret scanner has no opinion and neither did review. What leaks is topology and
identity: hostnames tell a stranger what to look for, private repo names tell
them where, and a first name tells them who.

THE DESIGN TRAP
---------------
A deny-list naming the real hosts CANNOT live in a public repo - that file would
BE the leak, published in the exact place it is meant to protect. So the check
is two layers:

  Layer 1  The patterns below match generic SHAPES, never specific names. They
           are safe to read publicly, they catch identifiers nobody has thought
           to add yet, and they need no credentials. That is why they are the
           default: a guard that needs credentials to run is a guard that gets
           skipped.
  Layer 2  Specific terms that have no generic shape (products, projects,
           PEOPLE) are fetched at runtime from SSM via --deny-list-ssm and are
           never stored here. Optional, because most repos have no AWS auth.

FAIL CLOSED, ALWAYS
-------------------
Every way this script can fail to do its job exits non-zero. It never reports
"clean" because it could not look:

  - `git diff` returning non-zero is fatal (an unresolvable range must not read
    as an empty diff, which would print "clean" and exit 0).
  - --require-changes makes an EMPTY diff fatal, so a caller that knows the PR
    changed files can refuse a scan that silently saw nothing.
  - An SSM read that errors, or resolves to an empty value, is fatal. Degrading
    to "generic shapes only" would mean the guard quietly stops covering the
    exact terms someone deliberately added to it.

EXIT CODES (a contract - the reusable workflow's warn mode depends on it)
    0  clean
    1  findings (a real hit; this is the only code a warn-mode caller may soften)
    2  usage or environment error (could not scan - never soften this)

USAGE
    check_private_leaks.py --staged                     # pre-commit: staged diff
    check_private_leaks.py --diff BASE...HEAD           # CI: a PR's diff
    check_private_leaks.py --text-file body.md          # an issue/PR body
    check_private_leaks.py --diff A...B --allow-repo-ref my-repo
    check_private_leaks.py --text-file b.md --deny-list-ssm /path/to/param

RELATIONSHIP TO grug's COPY
---------------------------
This started as quadseven/grug's scripts/check_private_leaks.py, the only leak
guard that existed in the fleet, and keeps its pattern semantics. Three
deliberate differences, all needed to make one script serve every repo:

  1. The "this repo's own issue refs are fine" exemption was hardcoded to one
     repo name. It is now --allow-repo-ref, supplied by the caller.
  2. The cross-repo-ref pattern missed the FULLY-QUALIFIED form (`owner/repo#1`
     slipped past the lookbehind entirely), which is the more revealing of the
     two. It now matches both forms.
  3. Two shapes were added for AGENTS.md bullets that had no pattern at all: a
     local user path (`/Users/<name>`) and a MAC address. Both measured zero
     hits across this repo's full history before being added.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# Generic shapes only. Every entry must be safe to read in a public repo, so
# describe the SHAPE of a private identifier, never an instance of one. Each
# maps to a bullet in templates/public-repo/AGENTS.md.
PATTERNS: list[tuple[str, str, str]] = [
    (
        "tailnet-host",
        r"\b[a-z0-9-]+\.ts\.[a-z0-9-]+\.[a-z]{2,}\b",
        "a tailnet hostname (private overlay network address)",
    ),
    (
        "tailscale-ip",
        r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b",
        "a CGNAT/Tailscale IP (the 100.64/10 range)",  # leak-guard-allow: RFC range, not a host
    ),
    (
        "rfc1918-ip",
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})\b",
        "a private-range IP address",
    ),
    (
        "server-hostname",
        r"\b(?:srv|usr)-[a-z0-9]+(?:-[a-z0-9]+)+\b",
        "an internal server hostname (srv-*/usr-* naming convention)",
    ),
    (
        "cluster-node",
        r"\bk8s-[a-z0-9]+-[a-z0-9-]*worker-\d+\b",
        "a Kubernetes worker-node name",
    ),
    (
        "local-user-path",
        # AGENTS.md: "a path that reveals a real local username". The lookahead
        # holds the accounts that are a CI runner or a container convention, not
        # a person - /home/runner/work is on every Actions log line - plus the
        # placeholder names docs are supposed to use.
        r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)"
        r"(?!(?:runner|root|ubuntu|user|username|node|vscode|git|app|appuser|"
        r"jenkins|actions|circleci|docker|linuxbrew|ec2-user|nobody|www-data|"
        r"you|me|name|example|your-name|placeholder)\b)"
        r"[a-z][a-z0-9._-]{1,30}\b",
        "a filesystem path containing a real local username",
    ),
    (
        "mac-address",
        r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b",
        "a MAC address (a real device identifier)",
    ),
]

# The cross-repo-ref rule is not in PATTERNS because it is not a constant: the
# caller's allow-list is spliced into it per run. Repos named there may be
# referenced as `repo#123`/`owner/repo#123`; anything else in that shape is
# treated as possibly naming a private repo. A bare `#123` is always this repo's
# own issue and is never matched.
REPO_REF_WHY = "a cross-repo issue reference (may name a private repo)"


def repo_ref_pattern(allowed: list[str]) -> str:
    """Build the cross-repo-ref regex with `allowed` exempted.

    Both forms are matched: bare `repo#1` and fully-qualified `owner/repo#1`.
    grug's original matched only the bare form - the lookbehind that stops
    `bar#1` matching inside `foo/bar#1` also meant a fully-qualified private ref
    sailed through untouched, and that is the form that names the owner too.

    The final char before `#` must be alphanumeric: prose like "Post-#77" is a
    hyphenated word followed by THIS repo's issue number, not a repo reference,
    and that shape was 91 of the first 99 hits when the rule was first written.
    """
    exempt = ""
    if allowed:
        exempt = "(?!(?:%s)\\b)" % "|".join(re.escape(a) for a in sorted(allowed))
    return (
        r"(?<![\w/-])"                       # not mid-word, not the tail of a path
        r"(?:[a-z0-9][a-z0-9._-]*/)?"        # optional owner/ prefix
        + exempt +
        r"[a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9]#\d+\b"
    )


# Lines that are ABOUT the guard rather than a leak. Without this the file
# documenting the patterns trips the patterns.
ALLOW_MARKERS = (
    "check_private_leaks",
    "leak-guard-allow",
    "PATTERNS:",
)


def build_patterns(allow_repo_refs: list[str] | None = None) -> list[tuple[str, re.Pattern[str], str]]:
    """Compile the generic-shape rules for one run."""
    rules = [(name, re.compile(pattern, re.I), why) for name, pattern, why in PATTERNS]
    rules.append((
        "private-issue-ref",
        re.compile(repo_ref_pattern(allow_repo_refs or []), re.I),
        REPO_REF_WHY,
    ))
    return rules


def load_ssm_deny_list(param: str) -> list[tuple[str, re.Pattern[str], str]]:
    """Specific names that have no generic shape (products, people, projects).

    Fetched at runtime, never written to a public repo. Both failure modes here
    are FATAL rather than a warning: silently degrading to "generic shapes only"
    would mean the guard quietly stops covering the exact terms someone
    deliberately added to it, which is indistinguishable from a green run.
    """
    out = subprocess.run(
        ["aws", "ssm", "get-parameter", "--name", param, "--with-decryption",
         "--query", "Parameter.Value", "--output", "text"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        print(f"FATAL: could not read deny-list from SSM {param}: "
              f"{out.stderr.strip()[:200]}", file=sys.stderr)
        raise SystemExit(2)
    terms = [t.strip() for t in out.stdout.split() if t.strip()]
    if not terms:
        # An empty parameter is not "no terms to check", it is a deny-list layer
        # that was asked for and did not arrive - a typo'd path, a wiped value,
        # a wrong region. Refuse rather than run half a guard.
        print(f"FATAL: deny-list SSM parameter {param} resolved to an empty "
              "value; refusing to run with the deny-list layer inert",
              file=sys.stderr)
        raise SystemExit(2)
    return [("deny-list", re.compile(re.escape(t), re.I), "an explicitly denied term")
            for t in terms]


def scan(text: str, rules, *, label: str, diff_mode: bool = False) -> list[str]:
    """Scan text for private identifiers.

    `diff_mode` is NOT cosmetic. In a diff, a line starting with `-` is a
    REMOVAL - i.e. someone scrubbing a leak - and blocking that would make the
    guard forbid its own remedy. In plain text (an issue or PR body) a leading
    `-` is a markdown bullet and must be scanned like any other line. The two
    cannot be told apart by looking, so the caller says which it has.
    """
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if diff_mode:
            if line.startswith(("---", "+++")):
                continue          # file headers carry paths, not content
            if line.startswith("-"):
                continue          # a removal is a scrub; never block it
            payload = line[1:] if line.startswith("+") else line
        else:
            payload = line
        if any(m in payload for m in ALLOW_MARKERS):
            continue
        for name, rx, why in rules:
            m = rx.search(payload)
            if m:
                hits.append(f"  {label}:{lineno}  [{name}] {m.group(0)!r} - {why}")
                break
    return hits


def git_diff(args: list[str]) -> str:
    """Run `git diff` and treat any failure as fatal.

    The failure this exists for: `subprocess.run(...).stdout` on a bad range
    returns an empty string, the scan finds nothing in it, and the guard prints
    "clean" and exits 0. A guard that cannot read the diff must go red, not
    green. errors="replace" so a file with non-UTF-8 bytes produces a scannable
    diff instead of a decode traceback.
    """
    out = subprocess.run(["git", "diff", "-U0", *args], capture_output=True,
                         text=True, errors="replace", check=False)
    if out.returncode != 0:
        print(f"FATAL: `git diff -U0 {' '.join(args)}` failed "
              f"(exit {out.returncode}): {out.stderr.strip()[:300]}",
              file=sys.stderr)
        raise SystemExit(2)
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--staged", action="store_true", help="scan the staged diff")
    src.add_argument("--diff", help="scan `git diff <RANGE>` (use BASE...HEAD)")
    src.add_argument("--text-file", help="scan a file (an issue/PR body)")
    ap.add_argument("--deny-list-ssm", help="SSM param holding extra terms")
    ap.add_argument("--allow-repo-ref", action="append", default=[], metavar="NAME",
                    help="repo name whose `name#123` refs are fine (repeatable); "
                         "callers pass at least their own repo's name")
    ap.add_argument("--require-changes", action="store_true",
                    help="with --diff/--staged: exit 2 if the diff is empty, so a "
                         "scan that saw nothing cannot report clean")
    args = ap.parse_args()

    rules = build_patterns(args.allow_repo_ref)
    if args.deny_list_ssm:
        rules += load_ssm_deny_list(args.deny_list_ssm)

    if args.staged:
        text = git_diff(["--cached"])
        label = "staged"
    elif args.diff:
        text = git_diff([args.diff])
        label = args.diff
    else:
        try:
            with open(args.text_file, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"FATAL: could not read {args.text_file}: {exc}", file=sys.stderr)
            return 2
        label = args.text_file

    if args.require_changes and not text.strip():
        print("FATAL: --require-changes was set but the diff is empty - the "
              "scan saw nothing, so it cannot report clean. Check the base/head "
              "refs are fetched (a shallow clone is the usual cause).",
              file=sys.stderr)
        return 2

    hits = scan(text, rules, label=label, diff_mode=args.staged or bool(args.diff))
    if not hits:
        print(f"leak guard: clean ({len(rules)} patterns, {len(text.splitlines())} lines scanned)")
        return 0

    print("BLOCKED: private infrastructure identifiers found\n", file=sys.stderr)
    for h in hits:
        print(h, file=sys.stderr)
    print(
        "\nThis repo is PUBLIC. Hostnames, node names, private-range IPs, real\n"
        "device identifiers, local user paths and cross-repo issue refs identify\n"
        "infrastructure and people even when they contain no secret. Describe the\n"
        "shape instead: 'an arm64 node', 'the infrastructure repo', 'a tailnet\n"
        "host'. See templates/public-repo/AGENTS.md for the full rule.\n"
        "\nIf a hit is genuinely fine, add the marker 'leak-guard-allow' to that\n"
        "line - deliberately noisy, so the exemption is visible in review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
