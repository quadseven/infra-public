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
    check_private_leaks.py --tree                       # every tracked file
    check_private_leaks.py --diff A...B --allow-repo-ref my-repo
    check_private_leaks.py --text-file b.md --deny-list-ssm /path/to/param

FULL-TREE MODE (--tree)
-----------------------
A diff scan only sees what a PR adds, so an identifier that was already in the
tree when the guard was switched on is never flagged. --tree scans every file
`git ls-files` reports (plus each path itself, since a file name can carry a
host name), for a scheduled sweep. Every hit is redacted, shape hits included:
the log of a sweep is public, and echoing each match would publish a neat
index of every private identifier left in the repo. The path and line number
are enough to find it. Binary files (a NUL byte in the first 8 KiB) are
skipped and counted. `git ls-files` failing, or listing no files, exits 2: a
sweep that looked at nothing must not report clean.

RELATIONSHIP TO grug's COPY
---------------------------
This started as quadseven/grug's scripts/check_private_leaks.py, the only leak
guard that existed in the fleet, and keeps its pattern semantics. Three
deliberate differences, all needed to make one script serve every repo:

  1. The "this repo's own issue refs are fine" exemption was hardcoded to one
     repo name. It is now --allow-repo-ref, supplied by the caller.
  2. The cross-repo-ref pattern missed the FULLY-QUALIFIED form (`<owner>/<repo>#1`
     slipped past the lookbehind entirely), which is the more revealing of the
     two. It now matches both forms.
  3. Two shapes were added for AGENTS.md bullets that had no pattern at all: a
     local user path (`/Users/<name>`) and a MAC address. Both measured zero
     hits across this repo's full history before being added.

Layer 2 (the deny-list) follows grug's semantics exactly, because the one real
deny-list in the estate is written in grug's format and a consumer pointed at
it must read it the same way:

  - ONE TERM PER LINE, blank lines and `#` comments dropped. Whitespace
    splitting would turn a full name into two unrelated single-word rules, and
    would turn every word of a `# comment` line into a rule that fires on
    ordinary prose.
  - Anchored on word-ish boundaries where the term's own edge is
    alphanumeric, so a short first name does not fire inside an ordinary word,
    while a path or domain fragment still matches mid-path.
  - A deny-list hit is NEVER echoed. The findings print to a PUBLIC Actions
    log; echoing the matched text would republish the protected term there and
    label it as protected. Only its length and line number are shown.
  - Every run prints how many shape patterns and deny-list terms it loaded, so
    "clean" always says whether the half that matches people was on.
"""

from __future__ import annotations

import argparse
import os
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
        # The (?<![\d.]) / (?!\.\d) guards keep a run of dotted numbers (an
        # SVG path's "7.86 10.92.58.11.79", a version string) from matching:
        # an address is exactly four octets, not four taken from a longer run.
        r"(?<![\d.])\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!\.\d)",
        "a CGNAT/Tailscale IP (the 100.64/10 range)",  # leak-guard-allow: RFC range, not a host
    ),
    (
        "rfc1918-ip",
        r"(?<![\d.])\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})\b(?!\.\d)",
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
        # The lookbehind keeps a URL path out: `github.com/users/<org>` is
        # a web page, not a home directory. A real path starts the token.
        r"(?:(?<![\w.])/Users/|(?<![\w.])/home/|[A-Za-z]:\\Users\\)"
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
    (
        "stray-mention",
        # AGENTS.md: "an @-mention of anyone who is not already part of the
        # conversation". The one exception to "shapes, never instances": this
        # handle is already public, it is the review bot's NAME but a real,
        # unrelated user's HANDLE, and agents keep writing it believing it
        # summons the bot. A backticked handle is a code span, not a mention,
        # so the backtick lookbehind keeps the policy text itself clean; the
        # lookbehind/ahead also keep an email address and a decorator clean.
        r"(?<![\w`.@/-])@grug(?![\w.-])",  # leak-guard-allow: the pattern itself
        "an @-mention of an unrelated real user (the bot is grug-tribe[bot], "
        "driven by /grug commands); a mention notifies and subscribes them",
    ),
]

# The cross-repo-ref rule is not in PATTERNS because it is not a constant: the
# caller's allow-list is spliced into it per run. Repos named there may be
# referenced as `<repo>#123`/`<owner>/<repo>#123`; anything else in that shape is
# treated as possibly naming a private repo. A bare `#123` is always this repo's
# own issue and is never matched.
REPO_REF_WHY = "a cross-repo issue reference (may name a private repo)"


def repo_ref_pattern(allowed: list[str]) -> str:
    """Build the cross-repo-ref regex with `allowed` exempted.

    Both forms are matched: bare `<repo>#1` and fully-qualified `<owner>/<repo>#1`.
    grug's original matched only the bare form - the lookbehind that stops
    the repo half of a qualified ref matching on its own also meant the whole
    qualified ref sailed through untouched - and that is the form that names the
    owner too.

    The final char before `#` must be alphanumeric: prose like "Post-#77" is a
    hyphenated word followed by THIS repo's issue number, not a repo reference,
    and that shape was 91 of the first 99 hits when the rule was first written.
    """
    # An ALL-CAPS name is a key prefix (`USER#1`, `INST#42`, `PR#734`), not a
    # repo: GitHub refs are written in lower or mixed case. `(?-i:...)` holds
    # this one check case-sensitive inside the case-insensitive rule.
    exempt = r"(?!(?-i:[A-Z][A-Z0-9_]*#))"
    if allowed:
        exempt += "(?!(?:%s)\\b)" % "|".join(re.escape(a) for a in sorted(allowed))
    return (
        r"(?<![\w/-])"  # not mid-word, not the tail of a path
        r"(?:[a-z0-9][a-z0-9._-]*/)?"  # optional owner/ prefix
        + exempt
        + r"[a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9]#\d+\b"
    )


# Lines that are ABOUT the guard rather than a leak. Without this the file
# documenting the patterns trips the patterns.
ALLOW_MARKERS = (
    "check_private_leaks",
    "leak-guard-allow",
    "PATTERNS:",
)


def build_patterns(
    allow_repo_refs: list[str] | None = None,
) -> list[tuple[str, re.Pattern[str], str]]:
    """Compile the generic-shape rules for one run."""
    rules = [(name, re.compile(pattern, re.I), why) for name, pattern, why in PATTERNS]
    rules.append(
        (
            "private-issue-ref",
            re.compile(repo_ref_pattern(allow_repo_refs or []), re.I),
            REPO_REF_WHY,
        )
    )
    return rules


DENY = "deny-list"  # rule name for layer-2 hits; scan() redacts these


def parse_deny_list(raw: str) -> list[str]:
    """One term per line; blanks and #-comments dropped; case-insensitive dedup.

    Line-based, NOT whitespace-split: a full name is one term, and splitting it
    would silently turn "Ada Lovelace" into two unrelated single-word rules.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for line in raw.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        terms.append(term)
    return terms


def deny_rule(term: str) -> re.Pattern[str]:
    """A denied term, anchored on word-ish boundaries.

    Plain substring matching is unusable for people: a four-letter first name
    is a substring of ordinary English words (`ada` sits inside `canada`), and
    a guard that fires on prose gets bypassed within a day. The boundary is
    applied only where the term's own edge is alphanumeric, so a path prefix or
    a domain suffix still matches mid-path and mid-hostname.
    """
    lead = r"(?<![0-9A-Za-z_])" if (term[0].isalnum() or term[0] == "_") else ""
    trail = r"(?![0-9A-Za-z_])" if (term[-1].isalnum() or term[-1] == "_") else ""
    return re.compile(lead + re.escape(term) + trail, re.I)


def load_ssm_deny_list(param: str) -> list[tuple[str, re.Pattern[str], str]]:
    """Specific names that have no generic shape (products, people, projects).

    Fetched at runtime, never written to a public repo. Both failure modes here
    are FATAL rather than a warning: silently degrading to "generic shapes only"
    would mean the guard quietly stops covering the exact terms someone
    deliberately added to it, which is indistinguishable from a green run.
    """
    try:
        out = subprocess.run(
            [
                "aws",
                "ssm",
                "get-parameter",
                "--name",
                param,
                "--with-decryption",
                "--query",
                "Parameter.Value",
                "--output",
                "text",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, UnicodeDecodeError) as exc:
        # No `aws` on PATH, or output that is not valid text. Without this the
        # interpreter dies with a traceback and exit 1 - which the exit-code
        # contract reads as "a leak was found", and which warn mode would
        # then soften to green. Not errors="replace": a mangled term would
        # silently stop matching, which is the degradation this refuses.
        print(
            f"FATAL: cannot read deny-list {param} via the AWS CLI: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    if out.returncode != 0:
        print(
            f"FATAL: could not read deny-list from SSM {param}: "
            f"{out.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    terms = parse_deny_list(out.stdout)
    if not terms:
        # An empty parameter is not "no terms to check", it is a deny-list layer
        # that was asked for and did not arrive - a typo'd path, a wiped value,
        # a wrong region. Refuse rather than run half a guard.
        print(
            f"FATAL: deny-list SSM parameter {param} resolved to an empty "
            "value; refusing to run with the deny-list layer inert",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return [(DENY, deny_rule(t), "an explicitly denied term") for t in terms]


def scan(
    text: str,
    rules,
    *,
    label: str,
    diff_mode: bool = False,
    redact_all: bool = False,
) -> list[str]:
    """Scan text for private identifiers.

    `diff_mode` is NOT cosmetic. In a diff, a line starting with `-` is a
    REMOVAL - i.e. someone scrubbing a leak - and blocking that would make the
    guard forbid its own remedy. In plain text (an issue or PR body) a leading
    `-` is a markdown bullet and must be scanned like any other line. The two
    cannot be told apart by looking, so the caller says which it has.

    `redact_all` hides shape hits too (full-tree mode; see the module doc).
    """
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if diff_mode:
            if line.startswith(("---", "+++")):
                continue  # file headers carry paths, not content
            if line.startswith("-"):
                continue  # a removal is a scrub; never block it
            payload = line[1:] if line.startswith("+") else line
        else:
            payload = line
        if any(m in payload for m in ALLOW_MARKERS):
            continue
        for name, rx, why in rules:
            m = rx.search(payload)
            if m:
                # A layer-2 hit is NEVER echoed: this prints to a PUBLIC
                # Actions log, and echoing the match would republish the very
                # term the deny-list exists to keep out. The line number is
                # enough - the author is looking at their own diff or text.
                shown = (
                    f"<redacted, {len(m.group(0))} chars>"
                    if name == DENY or redact_all
                    else repr(m.group(0))
                )
                hits.append(f"  {label}:{lineno}  [{name}] {shown} - {why}")
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
    out = subprocess.run(
        ["git", "diff", "-U0", *args],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        print(
            f"FATAL: `git diff -U0 {' '.join(args)}` failed "
            f"(exit {out.returncode}): {out.stderr.strip()[:300]}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return out.stdout


BINARY_SNIFF = 8192


def tree_files() -> list[str]:
    """Every tracked path, via `git ls-files -z`; any failure is fatal.

    An empty list is fatal too: outside a checkout, or on a checkout that
    fetched nothing, "no files" would otherwise print clean.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], capture_output=True, check=False
        )
    except OSError as exc:
        print(f"FATAL: cannot run `git ls-files`: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if out.returncode != 0:
        print(
            f"FATAL: `git ls-files` failed (exit {out.returncode}): "
            f"{out.stderr.decode('utf-8', 'replace').strip()[:300]}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    paths = [p.decode("utf-8", "replace") for p in out.stdout.split(b"\0") if p]
    if not paths:
        print(
            "FATAL: `git ls-files` listed no files - the sweep would scan "
            "nothing, so it cannot report clean.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return paths


def scan_tree(paths: list[str], rules) -> tuple[list[str], int, int]:
    """Scan each tracked file and each path. Returns (hits, lines, binaries).

    A tracked path that cannot be read is fatal rather than skipped: a sweep
    that quietly passes over a file it could not open has not swept it. A
    symlink is scanned as its target string (what git stores), and a
    submodule (a directory entry) is someone else's tree.
    """
    hits: list[str] = []
    lines = 0
    binaries = 0
    for index, path in enumerate(paths, 1):
        # A path that is itself a hit cannot be the label for its own
        # finding - that would print the name the redaction hides. It is
        # named by its position in `git ls-files` order instead.
        label = path
        if scan(path, rules, label=path):
            label = f"<tracked file #{index} in git ls-files order, name redacted>"
            hits += scan(path, rules, label=f"{label} (path)", redact_all=True)
        try:
            if os.path.islink(path):
                data = os.readlink(path).encode("utf-8", "replace")
            elif os.path.isdir(path):
                continue
            else:
                with open(path, "rb") as fh:
                    data = fh.read()
        except OSError as exc:
            print(f"FATAL: could not read tracked file {label}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if b"\0" in data[:BINARY_SNIFF]:
            binaries += 1
            continue
        text = data.decode("utf-8", "replace")
        lines += len(text.splitlines())
        hits += scan(text, rules, label=label, redact_all=True)
    return hits, lines, binaries


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--staged", action="store_true", help="scan the staged diff")
    src.add_argument("--diff", help="scan `git diff <RANGE>` (use BASE...HEAD)")
    src.add_argument("--text-file", help="scan a file (an issue/PR body)")
    src.add_argument(
        "--tree",
        action="store_true",
        help="scan every tracked file and path (scheduled sweep; all hits redacted)",
    )
    ap.add_argument("--deny-list-ssm", help="SSM param holding extra terms")
    ap.add_argument(
        "--allow-repo-ref",
        action="append",
        default=[],
        metavar="NAME",
        help="repo name whose `<name>#123` refs are fine (repeatable); "
        "callers pass at least their own repo's name",
    )
    ap.add_argument(
        "--require-changes",
        action="store_true",
        help="with --diff/--staged: exit 2 if the diff is empty, so a "
        "scan that saw nothing cannot report clean",
    )
    args = ap.parse_args()

    rules = build_patterns(args.allow_repo_ref)
    n_shapes = len(rules)
    n_deny = 0
    if args.deny_list_ssm:
        deny = load_ssm_deny_list(args.deny_list_ssm)
        n_deny = len(deny)
        rules += deny
    # Say what is loaded on EVERY run, pass or fail. grug's guard printed
    # "clean" for months while its people-matching half was never switched
    # on, and nothing in the output said so.
    coverage = (
        f"{n_shapes} shape patterns, {n_deny} deny-list terms"
        if args.deny_list_ssm
        else f"{n_shapes} shape patterns, deny-list not configured "
        "(names and products are not being checked)"
    )

    if args.tree:
        paths = tree_files()
        hits, n_lines, n_bin = scan_tree(paths, rules)
        scanned = (
            f"{len(paths)} tracked files, {n_lines} lines scanned, "
            f"{n_bin} binary files skipped"
        )
        if not hits:
            print(f"leak guard: clean ({coverage}, {scanned})")
            return 0
        print(
            f"BLOCKED: {len(hits)} private infrastructure identifier(s) in the "
            f"tree ({coverage}, {scanned}). Matches are redacted; open each "
            "path:line below in the repo.\n",
            file=sys.stderr,
        )
        for h in hits:
            print(h, file=sys.stderr)
        return 1

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
        print(
            "FATAL: --require-changes was set but the diff is empty - the "
            "scan saw nothing, so it cannot report clean. Check the base/head "
            "refs are fetched (a shallow clone is the usual cause).",
            file=sys.stderr,
        )
        return 2

    hits = scan(text, rules, label=label, diff_mode=args.staged or bool(args.diff))
    if not hits:
        print(f"leak guard: clean ({coverage}, {len(text.splitlines())} lines scanned)")
        return 0

    print(
        f"BLOCKED: private infrastructure identifiers found ({coverage})\n",
        file=sys.stderr,
    )
    for h in hits:
        print(h, file=sys.stderr)
    print(
        "\nThis repo is PUBLIC. Hostnames, node names, private-range IPs, real\n"
        "device identifiers, local user paths and cross-repo issue refs identify\n"
        "infrastructure and people even when they contain no secret. Describe the\n"
        "shape instead: 'an arm64 node', 'the infrastructure repo', 'a tailnet\n"
        "host'. See templates/public-repo/AGENTS.md for the full rule.\n"
        "\nA [deny-list] hit is a term with no generic shape - a person, a\n"
        "product, a host codename - and its text is deliberately NOT echoed\n"
        "here. Open the line above in your own diff to see it.\n"
        "\nIf a hit is genuinely fine, add the marker 'leak-guard-allow' to that\n"
        "line - deliberately noisy, so the exemption is visible in review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
