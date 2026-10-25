#!/usr/bin/env python3
"""Weekly fleet drift report across a list of repos (infra-public#167).

Shared CI settings drift between repos one bump at a time, and nothing says
so until a repo fails on the old value. This reads the workflow and
composite-action files of every listed repo, finds each shared setting,
takes the majority value across the repos that carry it, and writes one
issue per repo that differs, with file:line and the exact change.

Settings read, each keyed so the same thing in two repos compares:

- `action:<owner/repo[/path]>` - the version of every pinned `uses:`
  (the `# vX.Y.Z` label beside a SHA pin, else the ref itself).
- `runner:<ubuntu|macos|windows>` - a GitHub-hosted `runs-on:` image label.
- `<name>-version` - any `with:` input such as `python-version`.
- `with:<action>:version` - a `version:` input to an action (setup-uv...).
- `tool:<name>` - a tool pinned in a command (`ruff@0.15.10`,
  `ruff==0.15.10`, an actionlint release download).

Rules, in the order they decide an entry:

1. The majority is the value the most repos carry, and only exists when at
   least two repos carry it and no other value ties it.
2. A value older than the majority is a defect: the issue names the
   file:line and the replacement text.
3. A value newer than the majority is never asked to downgrade. The report
   lists it as ahead; the repo's issue does not mention it.
4. Anything else that differs is a judgment call, not a defect: no
   majority (a two-repo difference is always this), or two values that do
   not order (`ubuntu-latest` vs `ubuntu-24.04`, two bare SHAs). A judgment
   call never suggests a value older than the one the repo has.

Read-only across the listed repos. Issues go to one tracker (`--issue-repo`),
titled `Fleet drift: <repo>`, so a run updates its own issue instead of
filing a new one, and closes it once the repo is back in line.

Python stdlib only.

Run:
  python3 .github/scripts/fleet_drift.py fetch --repos a/b,c/d --out DIR
  python3 .github/scripts/fleet_drift.py report --root DIR --plan plan.json \
      --summary summary.md
  python3 .github/scripts/fleet_drift.py file --plan plan.json \
      --issue-repo owner/name [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

API = "https://api.github.com"
HTTP_TIMEOUT = 30
TITLE_PREFIX = "Fleet drift: "
LABEL = "fleet-drift"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")

USES_RE = re.compile(
    r"""^(\s*)(?:-\s+)?uses:\s*['"]?([^@\s'"]+)@([^\s'"#]+)['"]?\s*(?:#\s*(\S+))?"""
)
RUNS_ON_RE = re.compile(
    r"""^\s*runs-on:\s*['"]?(ubuntu|macos|windows)-(latest|\d+(?:\.\d+)*)['"]?\s*(?:#.*)?$"""
)
WITH_VERSION_RE = re.compile(
    r"""^(\s*)([a-z][a-z0-9]*-version):\s*['"]?([^'"\s#\[{$]+)['"]?\s*(?:#.*)?$"""
)
INPUT_VERSION_RE = re.compile(
    r"""^(\s*)version:\s*['"]?([^'"\s#\[{$]+)['"]?\s*(?:#.*)?$"""
)
TOOL_RE = re.compile(
    r"\b(ruff|uv|actionlint|mypy|pyright|black|pre-commit|shellcheck|yamllint|zizmor)"
    r"(?:@|==)v?(\d+(?:\.\d+)+)\b"
)
TOOL_DOWNLOAD_RE = re.compile(
    r"\b(?:[\w.-]+)/(actionlint|shellcheck)/releases/download/v?(\d+(?:\.\d+)+)\b"
)


@dataclass(frozen=True)
class Occurrence:
    repo: str
    key: str
    value: str
    path: str
    line: int
    text: str
    ref: str = ""  # the `@ref` of an action pin, so a change can name the SHA


@dataclass(frozen=True)
class Entry:
    """One difference worth writing down for one occurrence."""

    kind: str  # "defect" or "judgment"
    occ: Occurrence
    target: str  # the value to move to ("" when there is none to suggest)
    change: str  # the replacement text, or why there is no single answer


# ---------------------------------------------------------------- versions


def version_key(value: str) -> tuple[int, ...] | None:
    """The numeric tuple of a version-like value, or None if it does not order."""
    m = VERSION_RE.match(value)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def compare(a: str, b: str) -> int | None:
    """-1, 0 or 1 when a and b order; None when they do not.

    Two different strings that compare equal once padded (`v5` and
    `v5.0.0`) do not order: one is a floating tag, the other a release.
    """
    if a == b:
        return 0
    ka, kb = version_key(a), version_key(b)
    if ka is None or kb is None:
        return None
    width = max(len(ka), len(kb))
    pa = ka + (0,) * (width - len(ka))
    pb = kb + (0,) * (width - len(kb))
    if pa == pb:
        return None
    return -1 if pa < pb else 1


def runner_compare(a: str, b: str) -> int | None:
    """Order two runner labels of one family (`ubuntu-22.04` < `ubuntu-24.04`)."""
    va, vb = a.split("-", 1)[1], b.split("-", 1)[1]
    if a == b:
        return 0
    if "latest" in (va, vb):
        return None
    return compare(va, vb)


def cmp_for(key: str) -> Callable[[str, str], int | None]:
    return runner_compare if key.startswith("runner:") else compare


# ---------------------------------------------------------------- extraction


def _action_occurrence(
    repo: str, path: str, n: int, m: re.Match, stripped: str
) -> Occurrence | None:
    """The setting a `uses:` line pins, or None for a local or docker action."""
    action, ref, label = m.group(2), m.group(3), m.group(4) or ""
    if action.startswith(("./", "docker:")):
        return None
    value = label if label and version_key(label) else ref
    return Occurrence(repo, f"action:{action}", value, path, n, stripped, ref)


def _tool_settings(
    repo: str, path: str, n: int, line: str, stripped: str
) -> list[Occurrence]:
    """Tools pinned in a command: `ruff@X`, `ruff==X`, a release download."""
    found = [
        m for pattern in (TOOL_RE, TOOL_DOWNLOAD_RE) for m in pattern.finditer(line)
    ]
    return [
        Occurrence(repo, f"tool:{m.group(1)}", m.group(2), path, n, stripped)
        for m in found
    ]


def _plain_settings(
    repo: str, path: str, n: int, line: str, stripped: str, action: str
) -> list[Occurrence]:
    """Every setting on a non-`uses:` line; `action` is the step's action, if any."""
    m = RUNS_ON_RE.match(line)
    if m:
        label = f"{m.group(1)}-{m.group(2)}"
        return [Occurrence(repo, f"runner:{m.group(1)}", label, path, n, stripped)]
    m = WITH_VERSION_RE.match(line)
    if m:
        return [Occurrence(repo, m.group(2), m.group(3), path, n, stripped)]
    m = INPUT_VERSION_RE.match(line)
    if m:
        return (
            [Occurrence(repo, f"with:{action}:version", m.group(2), path, n, stripped)]
            if action
            else []
        )
    return _tool_settings(repo, path, n, line, stripped)


def extract(repo: str, path: str, text: str) -> list[Occurrence]:
    """Every shared setting in one workflow or action file."""
    out: list[Occurrence] = []
    current_action = ""
    action_indent = -1
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if current_action and len(line) - len(stripped) <= action_indent:
            current_action = ""
        m = USES_RE.match(line)
        if not m:
            out.extend(_plain_settings(repo, path, n, line, stripped, current_action))
            continue
        occ = _action_occurrence(repo, path, n, m, stripped)
        if occ is None:
            continue
        out.append(occ)
        current_action = occ.key.split(":", 1)[1]
        # A step's keys sit right of its dash (`- uses:`), or level with a
        # bare `uses:`; a line at or left of this column ends the step.
        action_indent = len(m.group(1)) - (0 if stripped.startswith("-") else 1)
    return out


def repo_files(repo_dir: Path) -> list[Path]:
    """The files one repo's settings are read from, in a stable order."""
    gh = repo_dir / ".github"
    files = sorted((gh / "workflows").glob("*.yml")) + sorted(
        (gh / "workflows").glob("*.yaml")
    )
    files += sorted((gh / "actions").glob("**/action.yml")) + sorted(
        (gh / "actions").glob("**/action.yaml")
    )
    return [f for f in files if f.is_file()]


def dir_to_repo(name: str) -> str:
    """Fetched repos are stored as `<owner>__<name>`."""
    return name.replace("__", "/", 1)


def repo_to_dir(repo: str) -> str:
    return repo.replace("/", "__", 1)


def scan(root: Path) -> tuple[list[str], list[Occurrence]]:
    repos: list[str] = []
    occs: list[Occurrence] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        repo = dir_to_repo(d.name)
        repos.append(repo)
        for f in repo_files(d):
            occs.extend(
                extract(
                    repo,
                    f.relative_to(d).as_posix(),
                    f.read_text(encoding="utf-8", errors="replace"),
                )
            )
    return repos, occs


# ---------------------------------------------------------------- analysis


def newest(values: list[str], key: str) -> str | None:
    """The one value every other value orders below, or None."""
    cmp = cmp_for(key)
    for v in values:
        if all(cmp(v, o) in (0, 1) for o in values):
            return v
    return None


def repo_value(values: list[str], key: str) -> str:
    """The value a repo carries for a key: its most common, ties to the newest."""
    counts = Counter(values)
    top = max(counts.values())
    tied = sorted(v for v, c in counts.items() if c == top)
    return newest(tied, key) or tied[0]


@dataclass
class Setting:
    key: str
    per_repo: dict[str, str]
    majority: str | None
    occurrences: list[Occurrence]
    # The most common `@ref` behind each action value, so a change can name a SHA.
    refs: dict[str, str] = field(default_factory=dict)


def settings(occs: list[Occurrence]) -> list[Setting]:
    by_key: dict[str, list[Occurrence]] = {}
    for o in occs:
        by_key.setdefault(o.key, []).append(o)
    out: list[Setting] = []
    for key in sorted(by_key):
        group = by_key[key]
        values_by_repo: dict[str, list[str]] = {}
        for o in group:
            values_by_repo.setdefault(o.repo, []).append(o.value)
        if len(values_by_repo) < 2:
            continue  # one repo carries it: nothing to compare against
        per_repo = {r: repo_value(v, key) for r, v in sorted(values_by_repo.items())}
        counts = Counter(per_repo.values()).most_common()
        majority = None
        if counts[0][1] >= 2 and (len(counts) == 1 or counts[1][1] < counts[0][1]):
            majority = counts[0][0]
        refs: dict[str, str] = {}
        if key.startswith("action:"):
            for v in {o.value for o in group}:
                c = Counter(o.ref for o in group if o.value == v)
                refs[v] = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append(Setting(key, per_repo, majority, group, refs))
    return out


def change_text(s: Setting, o: Occurrence, target: str) -> str:
    """`old line` -> `new line`, the exact edit that moves o to target."""
    if s.key.startswith("action:"):
        ref = s.refs.get(target, target)
        new = o.text.replace(f"@{o.ref}", f"@{ref}", 1)
        if o.value != o.ref and re.search(r"#\s*" + re.escape(o.value), new):
            new = re.sub(
                r"(#\s*)" + re.escape(o.value),
                lambda m: m.group(1) + target,
                new,
                count=1,
            )
        elif SHA_RE.match(ref):
            new = re.sub(r"\s*#.*$", "", new) + f"  # {target}"
        return f"`{o.text}` -> `{new}`"
    return f"`{o.text}` -> `{o.text.replace(o.value, target)}`"


AHEAD = "ahead"


def _against_majority(s: Setting, o: Occurrence) -> Entry | str | None:
    """Entry, AHEAD, or None (on the majority) for one occurrence."""
    if o.value == s.majority:
        return None
    c = cmp_for(s.key)(o.value, s.majority)
    if c == -1:
        return Entry("defect", o, s.majority, change_text(s, o, s.majority))
    if c == 1:
        return AHEAD
    return Entry(
        "judgment",
        o,
        "",
        f"`{o.value}` does not order against the majority `{s.majority}`",
    )


def _without_majority(
    s: Setting, o: Occurrence, best: str | None, values: list[str]
) -> Entry | None:
    """A judgment call, and never toward an older value."""
    if best is None:
        others = ", ".join(f"`{v}`" for v in values if v != o.value)
        return Entry("judgment", o, "", f"{no_majority(s)}; other repos carry {others}")
    if o.value == best or cmp_for(s.key)(o.value, best) in (0, 1):
        return None
    return Entry(
        "judgment",
        o,
        best,
        f"{no_majority(s)}; the newest in the fleet is {change_text(s, o, best)}",
    )


def analyse(
    sets: list[Setting],
) -> tuple[dict[str, list[Entry]], list[tuple[Setting, Occurrence]]]:
    """Per-repo entries, plus every occurrence that is ahead of the majority."""
    entries: dict[str, list[Entry]] = {}
    ahead: list[tuple[Setting, Occurrence]] = []
    for s in sets:
        values = sorted(set(s.per_repo.values()))
        best = newest(values, s.key)
        for o in s.occurrences:
            if s.majority is not None:
                got = _against_majority(s, o)
            else:
                got = _without_majority(s, o, best, values)
            if got == AHEAD:
                ahead.append((s, o))
            elif isinstance(got, Entry):
                entries.setdefault(o.repo, []).append(got)
    for items in entries.values():
        items.sort(key=lambda e: (e.occ.path, e.occ.line, e.occ.key))
    return entries, ahead


def no_majority(s: Setting) -> str:
    n = len(s.per_repo)
    return (
        "no majority (only two repos carry it)"
        if n == 2
        else f"no majority across the {n} repos that carry it"
    )


# ---------------------------------------------------------------- rendering


def fmt_repo_values(s: Setting) -> str:
    """Each repo's value with the file:line it was read from."""
    parts = []
    for r, v in s.per_repo.items():
        o = next(o for o in s.occurrences if o.repo == r and o.value == v)
        parts.append(f"{r}: `{v}` ({o.path}:{o.line})")
    return "; ".join(parts)


def render_summary(
    repos: list[str],
    sets: list[Setting],
    entries: dict[str, list[Entry]],
    ahead: list[tuple[Setting, Occurrence]],
) -> str:
    lines = ["# Fleet drift report", "", f"Repos read: {', '.join(repos)}.", ""]
    lines += ["| Setting | Majority | Per repo |", "| --- | --- | --- |"]
    for s in sets:
        maj = f"`{s.majority}`" if s.majority else "none"
        lines.append(f"| `{s.key}` | {maj} | {fmt_repo_values(s)} |")
    lines.append("")
    lines.append("## Differences")
    lines.append("")
    if not entries and not ahead:
        lines.append("Every shared setting matches across the fleet.")
    for repo in sorted(entries):
        for e in entries[repo]:
            lines.append(f"- {repo} `{e.occ.path}:{e.occ.line}` {e.kind}: {e.change}")
    for s, o in ahead:
        lines.append(
            f"- {o.repo} `{o.path}:{o.line}` ahead of the majority `{s.majority}` "
            f"with `{o.value}`; not asked to downgrade"
        )
    lines.append("")
    return "\n".join(lines)


def render_issue(repo: str, items: list[Entry]) -> str:
    defects = [e for e in items if e.kind == "defect"]
    judgments = [e for e in items if e.kind == "judgment"]
    out = [
        f"The weekly fleet drift report found {len(defects)} setting(s) in {repo} "
        f"behind the fleet majority and {len(judgments)} judgment call(s).",
        "",
        "A newer pin than the majority is never listed here: the fleet catches up "
        "to it, not the other way round.",
        "",
    ]
    if defects:
        out += ["## Behind the majority", ""]
        out += [
            f"- [ ] `{e.occ.path}:{e.occ.line}` `{e.occ.key}`: {e.change}"
            for e in defects
        ]
        out.append("")
    if judgments:
        out += [
            "## Judgment calls",
            "",
            "No clear majority or no order between the values, so these are "
            "for a person to decide, not defects.",
            "",
        ]
        out += [
            f"- `{e.occ.path}:{e.occ.line}` `{e.occ.key}`: {e.change}"
            for e in judgments
        ]
        out.append("")
    out.append(
        "This issue is rewritten by every run and closed by the first run that "
        "finds no difference."
    )
    return "\n".join(out) + "\n"


def build_plan(root: Path) -> tuple[dict, str]:
    repos, occs = scan(root)
    sets = settings(occs)
    entries, ahead = analyse(sets)
    issues = [
        {"repo": r, "title": TITLE_PREFIX + r, "body": render_issue(r, entries[r])}
        for r in sorted(entries)
    ]
    clean = [r for r in repos if r not in entries]
    plan = {"repos": repos, "issues": issues, "clean": clean}
    return plan, render_summary(repos, sets, entries, ahead)


# ---------------------------------------------------------------- GitHub I/O


class GitHub:
    """A minimal REST client. `opener` is swappable so tests never touch the network."""

    def __init__(self, token: str, opener: Callable | None = None):
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def call(self, method: str, path: str, body: dict | None = None):
        if not path.startswith("/"):
            raise ValueError(f"not an API path: {path!r}")
        # API + an absolute path: the scheme is always https, never file: or
        # a caller-chosen one, which is what S310 guards against.
        url = API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self.opener(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None


WANTED_RE = re.compile(r"^\.github/(workflows/[^/]+\.ya?ml|actions/.+/action\.ya?ml)$")


def fetch(gh: GitHub, repos: list[str], out: Path) -> None:
    """Copy each repo's workflow and action files from its default branch. Read-only."""
    for repo in repos:
        q = urllib.parse.quote
        meta = gh.call("GET", f"/repos/{repo}")
        branch = meta["default_branch"]
        tree = gh.call(
            "GET", f"/repos/{repo}/git/trees/{q(branch, safe='')}?recursive=1"
        )
        dest = out / repo_to_dir(repo)
        dest.mkdir(parents=True, exist_ok=True)
        for item in tree.get("tree", []):
            if item.get("type") != "blob" or not WANTED_RE.match(item.get("path", "")):
                continue
            blob = gh.call("GET", f"/repos/{repo}/git/blobs/{item['sha']}")
            target = dest / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(blob["content"]))


def _open_drift_issues(gh: GitHub, issue_repo: str) -> dict[str, int]:
    """Title -> number of every open `Fleet drift:` issue (not PR) in the tracker."""
    existing: dict[str, int] = {}
    page = 1
    while True:
        batch = (
            gh.call(
                "GET",
                f"/repos/{issue_repo}/issues?state=open&labels={LABEL}&per_page=100&page={page}",
            )
            or []
        )
        for it in batch:
            if "pull_request" not in it and it["title"].startswith(TITLE_PREFIX):
                existing[it["title"]] = it["number"]
        if len(batch) < 100:
            return existing
        page += 1


def _ensure_label(gh: GitHub, issue_repo: str) -> None:
    try:
        gh.call(
            "POST",
            f"/repos/{issue_repo}/labels",
            {
                "name": LABEL,
                "color": "c5def5",
                "description": "Weekly fleet drift report",
            },
        )
    except urllib.error.HTTPError as e:
        e.close()
        if e.code != 422:  # 422: the label already exists
            raise


def _planned_writes(
    plan: dict, issue_repo: str, existing: dict[str, int]
) -> list[tuple[str, list[tuple]]]:
    """(description, [(method, path, body), ...]) for every issue change, in order."""
    base = f"/repos/{issue_repo}/issues"
    out: list[tuple[str, list[tuple]]] = []
    for issue in plan["issues"]:
        num = existing.get(issue["title"])
        if num:
            out.append(
                (
                    f"update #{num} {issue['title']}",
                    [("PATCH", f"{base}/{num}", {"body": issue["body"]})],
                )
            )
        else:
            body = {"title": issue["title"], "body": issue["body"], "labels": [LABEL]}
            out.append((f"create {issue['title']}", [("POST", base, body)]))
    for repo in plan["clean"]:
        title = TITLE_PREFIX + repo
        num = existing.get(title)
        if num:
            note = {
                "body": "The fleet drift report finds no difference for this repo "
                "any more. Closing."
            }
            close = {"state": "closed", "state_reason": "completed"}
            out.append(
                (
                    f"close #{num} {title}",
                    [
                        ("POST", f"{base}/{num}/comments", note),
                        ("PATCH", f"{base}/{num}", close),
                    ],
                )
            )
    return out


def file_issues(
    gh: GitHub, plan: dict, issue_repo: str, dry_run: bool, log=print
) -> list[str]:
    """Create, rewrite or close one `Fleet drift: <repo>` issue per listed repo."""
    writes = _planned_writes(plan, issue_repo, _open_drift_issues(gh, issue_repo))
    if plan["issues"] and not dry_run:
        _ensure_label(gh, issue_repo)
    for what, calls in writes:
        log(("dry run: would " if dry_run else "") + what)
        if not dry_run:
            for method, path, body in calls:
                gh.call(method, path, body)
    return [what for what, _ in writes]


def parse_repos(text: str) -> list[str]:
    repos = [r.strip() for r in re.split(r"[\s,]+", text) if r.strip()]
    for r in repos:
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", r):
            raise SystemExit(f"not an owner/name repo: {r!r}")
    return repos


def main(argv: list[str] | None = None, opener: Callable | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument(
        "--repos", required=True, help="owner/name list, comma or whitespace separated"
    )
    f.add_argument("--out", required=True, type=Path)
    r = sub.add_parser("report")
    r.add_argument("--root", required=True, type=Path)
    r.add_argument("--plan", required=True, type=Path)
    r.add_argument("--summary", type=Path)
    i = sub.add_parser("file")
    i.add_argument("--plan", required=True, type=Path)
    i.add_argument("--issue-repo", required=True)
    i.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    token = os.environ.get("GH_TOKEN", "")
    if args.cmd == "fetch":
        fetch(GitHub(token, opener), parse_repos(args.repos), args.out)
        return 0
    if args.cmd == "report":
        plan, summary = build_plan(args.root)
        args.plan.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        if args.summary:
            args.summary.write_text(summary, encoding="utf-8")
        print(summary)
        return 0
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    file_issues(GitHub(token, opener), plan, args.issue_repo, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
