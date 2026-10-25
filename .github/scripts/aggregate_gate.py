#!/usr/bin/env python3
"""Aggregate gate: one required check that fails closed (infra-public#166).

A ruleset that requires ten checks by name has ten ways to be wrong: a check
that is renamed, path-filtered away, skipped by an `if:`, or never queued
leaves the pull request mergeable, because GitHub only blocks on a required
check that REPORTS a failure. This script backs one check that a ruleset can
require on its own, and that is green only when every other leg really
passed.

Two modes, one rule (anything that is not a proven success is a failure):

fold   The caller runs its gate workflows as jobs and passes `toJSON(needs)`.
       Every leg must be `success`. `failure`, `cancelled`, an empty or
       unknown result, and `skipped` (unless the leg is listed in
       --allow-skipped) fail the gate. A leg named in --expected that is not
       in the results at all fails it too, and so does an empty result set.

poll   The gate runs as its own workflow and reads the commit's check runs
       and commit statuses from the API, waiting for the others to finish.
       It ignores every check run of its own workflow run (by run id in the
       check's details URL) plus any name matching --ignore. It fails as soon
       as any other check concludes with something other than success, when
       a check is still queued after --start-timeout ("never started"), when
       a name in --expected has not appeared by --start-timeout, or when
       anything is still running at --timeout. It passes only once every
       other check is complete and green, every expected name is present,
       and at least --settle seconds have passed (so a workflow that queues
       a little after the gate is still seen).

Python stdlib only, so the reusable workflow needs nothing installed.

Run:  python3 aggregate_gate.py fold   (reads NEEDS_JSON from the env)
      python3 aggregate_gate.py poll   (reads GITHUB_TOKEN, GITHUB_REPOSITORY,
                                        GITHUB_RUN_ID, GITHUB_API_URL, GATE_SHA)
Exit 0 = pass, 1 = the gate failed, 2 = the gate could not run (bad input).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

# Check-run conclusions that can never count as a pass.
FAILING_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
}
# Check-run statuses that mean "has not started running yet".
NOT_STARTED_STATUSES = {"queued", "waiting", "requested", "pending"}


class GateInputError(Exception):
    """The gate was given input it cannot evaluate. Fails the check (exit 2)."""


def parse_names(text: str | None) -> list[str]:
    """Split a comma- or newline-separated list; blank entries and # comments drop."""
    if not text:
        return []
    names = []
    for line in text.replace(",", "\n").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


# ---------------------------------------------------------------- fold mode


def evaluate_needs(
    needs: object, expected: list[str], allow_skipped: list[str]
) -> list[str]:
    """Return one problem line per leg that is not a proven success.

    `needs` is the parsed `toJSON(needs)` of the caller's gate job: a mapping
    of job id to an object with a `result` key.
    """
    if not isinstance(needs, dict):
        raise GateInputError(
            "needs-json is not a JSON object; pass ${{ toJSON(needs) }}"
        )
    problems: list[str] = []
    if not needs:
        problems.append(
            "no legs: the gate job has an empty `needs:` list, so it proves nothing"
        )
    for name in expected:
        if name not in needs:
            problems.append(f"{name}: expected leg is missing from the results")
    for name in sorted(needs):
        leg = needs[name]
        result = leg.get("result") if isinstance(leg, dict) else None
        if result == "success":
            continue
        if result == "skipped" and matches(name, allow_skipped):
            continue
        if result == "skipped":
            problems.append(
                f"{name}: skipped (list it in allow-skipped if skipping is correct)"
            )
        elif not result:
            problems.append(f"{name}: no result reported")
        else:
            problems.append(f"{name}: {result}")
    return problems


# ---------------------------------------------------------------- poll mode


@dataclass
class Snapshot:
    """What the API said about the commit at one moment."""

    check_runs: list[dict] = field(default_factory=list)
    statuses: list[dict] = field(default_factory=list)


@dataclass
class Verdict:
    state: str  # "pass", "fail" or "pending"
    problems: list[str] = field(default_factory=list)
    waiting_on: list[str] = field(default_factory=list)


def is_own_run(check: dict, run_id: str) -> bool:
    if not run_id:
        return False
    marker = f"/actions/runs/{run_id}/"
    for key in ("details_url", "html_url"):
        url = check.get(key) or ""
        if marker in url:
            return True
    return False


def latest_check_runs(check_runs: list[dict]) -> list[dict]:
    """Keep the newest check run per (check suite, name); a re-run supersedes."""
    best: dict[tuple, dict] = {}
    for cr in check_runs:
        suite = (cr.get("check_suite") or {}).get("id")
        key = (suite, cr.get("name"))
        if key not in best or (cr.get("id") or 0) > (best[key].get("id") or 0):
            best[key] = cr
    return list(best.values())


def _judge_check_run(cr: dict, elapsed: float, rules: dict) -> tuple[str, str]:
    """One check run -> ("ok" | "wait" | "fail", detail)."""
    name = cr.get("name") or "<unnamed check>"
    status = cr.get("status")
    if status != "completed":
        if status in NOT_STARTED_STATUSES and elapsed >= rules["start_timeout"]:
            return "fail", f"{name}: never started ({status} after {int(elapsed)}s)"
        return "wait", name
    conclusion = cr.get("conclusion")
    if conclusion == "success":
        return "ok", name
    if conclusion == "skipped":
        if matches(name, rules["allow_skipped"]):
            return "ok", name
        return "fail", f"{name}: skipped (list it in allow-skipped if skipping is correct)"
    if conclusion == "neutral":
        if rules["allow_neutral"]:
            return "ok", name
        return "fail", f"{name}: neutral"
    if conclusion in FAILING_CONCLUSIONS:
        return "fail", f"{name}: {conclusion}"
    return "fail", f"{name}: completed with no usable conclusion ({conclusion})"


def _judge_status(st: dict) -> tuple[str, str]:
    """One commit status -> ("ok" | "wait" | "fail", detail)."""
    name = st.get("context") or "<unnamed status>"
    state = st.get("state")
    if state == "success":
        return "ok", name
    if state == "pending":
        return "wait", name
    return "fail", f"{name}: commit status {state}"


def evaluate_checks(
    snapshot: Snapshot,
    *,
    run_id: str,
    expected: list[str],
    ignore: list[str],
    allow_skipped: list[str],
    allow_neutral: bool,
    elapsed: float,
    start_timeout: float,
    settle: float,
) -> Verdict:
    rules = {
        "allow_skipped": allow_skipped,
        "allow_neutral": allow_neutral,
        "start_timeout": start_timeout,
    }
    judged: list[tuple[str, str, str]] = []  # (name, outcome, detail)
    for cr in latest_check_runs(snapshot.check_runs):
        name = cr.get("name") or "<unnamed check>"
        if not is_own_run(cr, run_id) and not matches(name, ignore):
            judged.append((name, *_judge_check_run(cr, elapsed, rules)))
    for st in snapshot.statuses:
        name = st.get("context") or "<unnamed status>"
        if not matches(name, ignore):
            judged.append((name, *_judge_status(st)))

    seen = [name for name, _, _ in judged]
    problems = [d for _, outcome, d in judged if outcome == "fail"]
    waiting = [d for _, outcome, d in judged if outcome == "wait"]
    for pattern in expected:
        if any(fnmatch.fnmatchcase(s, pattern) for s in seen):
            continue
        if elapsed >= start_timeout:
            problems.append(f"{pattern}: expected check never started ({int(elapsed)}s)")
        else:
            waiting.append(f"{pattern} (not started yet)")

    if problems:
        return Verdict("fail", problems, waiting)
    if waiting:
        return Verdict("pending", [], waiting)
    if elapsed < settle:
        return Verdict("pending", [], [f"settling ({int(elapsed)}s of {int(settle)}s)"])
    if not seen:
        return Verdict(
            "fail",
            ["no other checks reported on this commit; the gate proves nothing"],
            [],
        )
    return Verdict("pass")


def poll(
    fetch: Callable[[], Snapshot],
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    interval: float,
    timeout: float,
    log: Callable[[str], None],
    **rules,
) -> Verdict:
    """Fetch and evaluate until the verdict is final or `timeout` passes.

    A failing verdict is confirmed by one more read an interval later before
    it is returned: some apps post a check as completed and then rewrite its
    conclusion (a code-scanning check can read `neutral` for a few seconds
    before it settles on `success`). A failure that is still there on the
    second read is final.
    """
    start = clock()
    last_waiting: list[str] | None = None
    unconfirmed: Verdict | None = None
    while True:
        elapsed = clock() - start
        verdict = evaluate_checks(fetch(), elapsed=elapsed, **rules)
        if verdict.state == "pass":
            return verdict
        if verdict.state == "fail":
            if unconfirmed is not None:
                return verdict
            log(f"failure seen, confirming on the next read: {'; '.join(verdict.problems)}")
            unconfirmed = verdict
            sleep(interval)
            continue
        unconfirmed = None
        if elapsed >= timeout:
            return Verdict(
                "fail",
                [f"{w}: still not finished at the {int(timeout)}s timeout" for w in verdict.waiting_on],
                verdict.waiting_on,
            )
        if verdict.waiting_on != last_waiting:
            log(f"waiting on: {', '.join(verdict.waiting_on)}")
            last_waiting = verdict.waiting_on
        sleep(interval)


class GitHubApi:
    def __init__(self, api_url: str, repo: str, token: str):
        # Only https: the token is sent on every request, and urlopen would
        # otherwise also accept file: or a custom scheme.
        if not api_url.startswith("https://"):
            raise GateInputError(f"GITHUB_API_URL must be an https URL (got {api_url!r})")
        self.api_url = api_url.rstrip("/")
        self.repo = repo
        self.token = token

    def _get(self, path: str) -> object:
        req = urllib.request.Request(  # noqa: S310 - scheme checked https in __init__
            f"{self.api_url}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - https only, see __init__
            return json.load(resp)

    def snapshot(self, sha: str) -> Snapshot:
        runs: list[dict] = []
        page = 1
        while True:
            data = self._get(
                f"/repos/{self.repo}/commits/{sha}/check-runs"
                f"?filter=latest&per_page=100&page={page}"
            )
            batch = data.get("check_runs", []) if isinstance(data, dict) else []
            runs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        status = self._get(f"/repos/{self.repo}/commits/{sha}/status?per_page=100")
        statuses = status.get("statuses", []) if isinstance(status, dict) else []
        return Snapshot(runs, statuses)


def fetch_with_retry(fetch: Callable[[], Snapshot], log: Callable[[str], None]) -> Callable[[], Snapshot]:
    """A transient API error is retried; three in a row fail the gate (closed)."""

    def wrapped() -> Snapshot:
        for attempt in range(3):
            try:
                return fetch()
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                log(f"API read failed (attempt {attempt + 1} of 3): {exc}")
                time.sleep(5 * (attempt + 1))
        raise GateInputError("could not read the commit's checks from the API")

    return wrapped


# ---------------------------------------------------------------- CLI


def report(problems: list[str], ok_message: str) -> int:
    if problems:
        for p in problems:
            print(f"::error title=aggregate gate::{p}")
        print(f"aggregate gate FAILED: {len(problems)} problem(s)")
        return EXIT_FAIL
    print(ok_message)
    return EXIT_PASS


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    f = sub.add_parser("fold", help="evaluate the caller's toJSON(needs)")
    f.add_argument("--expected", default=env.get("GATE_EXPECTED", ""))
    f.add_argument("--allow-skipped", default=env.get("GATE_ALLOW_SKIPPED", ""))
    p = sub.add_parser("poll", help="wait for the commit's other checks")
    p.add_argument("--expected", default=env.get("GATE_EXPECTED", ""))
    p.add_argument("--allow-skipped", default=env.get("GATE_ALLOW_SKIPPED", ""))
    p.add_argument("--ignore", default=env.get("GATE_IGNORE", ""))
    p.add_argument("--allow-neutral", default=env.get("GATE_ALLOW_NEUTRAL", "false"))
    p.add_argument("--interval", type=float, default=float(env.get("GATE_INTERVAL", "30")))
    p.add_argument("--timeout", type=float, default=float(env.get("GATE_TIMEOUT", "3600")))
    p.add_argument("--start-timeout", type=float, default=float(env.get("GATE_START_TIMEOUT", "600")))
    p.add_argument("--settle", type=float, default=float(env.get("GATE_SETTLE", "60")))
    args = parser.parse_args(argv)

    runner = _run_fold if args.mode == "fold" else _run_poll
    try:
        return runner(args, env)
    except GateInputError as exc:
        print(f"::error title=aggregate gate::cannot evaluate: {exc}")
        return EXIT_ERROR


def _run_fold(args: argparse.Namespace, env) -> int:
    raw = env.get("NEEDS_JSON", "")
    if not raw.strip():
        raise GateInputError("NEEDS_JSON is empty; pass needs-json: toJSON(needs)")
    try:
        needs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateInputError(f"NEEDS_JSON is not valid JSON: {exc}") from exc
    problems = evaluate_needs(needs, parse_names(args.expected), parse_names(args.allow_skipped))
    for name in sorted(needs):
        leg = needs[name]
        print(f"{name}: {leg.get('result') if isinstance(leg, dict) else leg}")
    return report(problems, f"aggregate gate passed: {len(needs)} leg(s) succeeded")


def _run_poll(args: argparse.Namespace, env) -> int:
    missing = [k for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GATE_SHA") if not env.get(k)]
    if missing:
        raise GateInputError(f"missing environment: {', '.join(missing)}")
    if args.start_timeout > args.timeout:
        raise GateInputError("start-timeout is longer than timeout")
    api = GitHubApi(
        env.get("GITHUB_API_URL", "https://api.github.com"),
        env["GITHUB_REPOSITORY"],
        env["GITHUB_TOKEN"],
    )
    sha = env["GATE_SHA"]
    print(f"aggregate gate: waiting on the checks of {sha}")
    verdict = poll(
        fetch_with_retry(lambda: api.snapshot(sha), print),
        clock=time.monotonic,
        sleep=time.sleep,
        interval=args.interval,
        timeout=args.timeout,
        log=print,
        run_id=env.get("GITHUB_RUN_ID", ""),
        expected=parse_names(args.expected),
        ignore=parse_names(args.ignore),
        allow_skipped=parse_names(args.allow_skipped),
        allow_neutral=str(args.allow_neutral).lower() == "true",
        start_timeout=args.start_timeout,
        settle=args.settle,
    )
    return report(verdict.problems, "aggregate gate passed: every other check succeeded")


if __name__ == "__main__":
    sys.exit(main())
