#!/usr/bin/env python3
"""Unit suite for aggregate_gate.py and _reusable.aggregate-gate.yml
(infra-public#166).

The direction that matters is the failing one. A gate that goes green on a
cancelled, skipped or missing leg is worse than no gate, because the ruleset
now trusts it alone - so most cases here assert a FAIL.

The last class reads the workflow files as text: the reusable, both
canaries and the example caller must keep the shape the gate depends on
(`if: always()`, every leg in `needs:`, needs passed through env).

Python stdlib only (unittest), matching the other guard suites here.

Run: python3 .github/scripts/aggregate_gate_test.py
"""

from __future__ import annotations

import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aggregate_gate import (  # noqa: E402
    EXIT_ERROR,
    EXIT_FAIL,
    EXIT_PASS,
    GateInputError,
    Snapshot,
    evaluate_checks,
    evaluate_needs,
    main,
    parse_names,
    poll,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REUSABLE = WORKFLOWS / "_reusable.aggregate-gate.yml"
FOLD_CANARY = WORKFLOWS / "canary.aggregate-gate.yml"
POLL_CANARY = WORKFLOWS / "canary.aggregate-gate-poll.yml"
EXAMPLE = REPO_ROOT / "examples" / "aggregate-gate" / "ci-gate.yml"

RUN_ID = "4242"
OWN_URL = f"https://github.com/o/r/actions/runs/{RUN_ID}/job/1"


def leg(result):
    return {"result": result, "outputs": {}}


def check(name, status="completed", conclusion="success", cid=1, suite=1, url=None):
    return {
        "id": cid,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "check_suite": {"id": suite},
        "details_url": url or f"https://github.com/o/r/actions/runs/99/job/{cid}",
    }


RULES = dict(
    run_id=RUN_ID,
    expected=[],
    ignore=[],
    allow_skipped=[],
    allow_neutral=False,
    start_timeout=600,
    settle=0,
)


def verdict(runs, statuses=(), elapsed=100, **over):
    rules = {**RULES, **over}
    return evaluate_checks(Snapshot(list(runs), list(statuses)), elapsed=elapsed, **rules)


class ParseNames(unittest.TestCase):
    def test_commas_newlines_comments(self):
        self.assertEqual(parse_names("a, b\n c # note\n\n#x\n"), ["a", "b", "c"])

    def test_empty(self):
        self.assertEqual(parse_names(""), [])
        self.assertEqual(parse_names(None), [])


class Fold(unittest.TestCase):
    def test_all_success_passes(self):
        self.assertEqual(evaluate_needs({"a": leg("success"), "b": leg("success")}, [], []), [])

    def test_failure_fails(self):
        p = evaluate_needs({"a": leg("success"), "b": leg("failure")}, [], [])
        self.assertEqual(p, ["b: failure"])

    def test_cancelled_fails(self):
        self.assertEqual(evaluate_needs({"a": leg("cancelled")}, [], []), ["a: cancelled"])

    def test_skipped_fails_unless_allowed(self):
        self.assertEqual(len(evaluate_needs({"a": leg("skipped")}, [], [])), 1)
        self.assertEqual(evaluate_needs({"a": leg("skipped")}, [], ["a"]), [])

    def test_allow_skipped_does_not_excuse_a_failure(self):
        self.assertEqual(evaluate_needs({"a": leg("failure")}, [], ["a"]), ["a: failure"])

    def test_allow_skipped_globs(self):
        self.assertEqual(evaluate_needs({"deploy-x": leg("skipped")}, [], ["deploy-*"]), [])

    def test_missing_expected_leg_fails(self):
        p = evaluate_needs({"a": leg("success")}, ["a", "b"], [])
        self.assertEqual(p, ["b: expected leg is missing from the results"])

    def test_empty_or_unknown_result_fails(self):
        self.assertEqual(evaluate_needs({"a": {}}, [], []), ["a: no result reported"])
        self.assertEqual(evaluate_needs({"a": leg("")}, [], []), ["a: no result reported"])
        self.assertEqual(evaluate_needs({"a": "success"}, [], []), ["a: no result reported"])
        self.assertEqual(evaluate_needs({"a": leg("weird")}, [], []), ["a: weird"])

    def test_no_legs_fails(self):
        self.assertEqual(len(evaluate_needs({}, [], [])), 1)

    def test_not_an_object_is_an_input_error(self):
        with self.assertRaises(GateInputError):
            evaluate_needs(["a"], [], [])


class PollEvaluate(unittest.TestCase):
    def test_all_green_passes(self):
        self.assertEqual(verdict([check("a"), check("b", cid=2)]).state, "pass")

    def test_each_failing_conclusion_fails(self):
        for c in ("failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure", None, "odd"):
            with self.subTest(conclusion=c):
                v = verdict([check("a"), check("b", conclusion=c, cid=2)])
                self.assertEqual(v.state, "fail")

    def test_failure_fails_fast_while_others_still_run(self):
        v = verdict([check("a", conclusion="failure"), check("b", status="in_progress", cid=2)])
        self.assertEqual(v.state, "fail")

    def test_skipped_fails_unless_allowed(self):
        self.assertEqual(verdict([check("a", conclusion="skipped")]).state, "fail")
        self.assertEqual(verdict([check("a", conclusion="skipped")], allow_skipped=["a"]).state, "pass")
        self.assertEqual(
            verdict([check("a", conclusion="skipped"), check("b", cid=2)], allow_skipped=["a"]).state,
            "pass",
        )

    def test_neutral_fails_unless_allowed(self):
        self.assertEqual(verdict([check("a", conclusion="neutral")]).state, "fail")
        self.assertEqual(verdict([check("a", conclusion="neutral")], allow_neutral=True).state, "pass")

    def test_running_is_pending(self):
        self.assertEqual(verdict([check("a", status="in_progress")]).state, "pending")

    def test_queued_past_start_timeout_never_started(self):
        v = verdict([check("a", status="queued")], elapsed=601)
        self.assertEqual(v.state, "fail")
        self.assertIn("never started", v.problems[0])
        self.assertEqual(verdict([check("a", status="queued")], elapsed=10).state, "pending")

    def test_running_past_start_timeout_still_pending(self):
        self.assertEqual(verdict([check("a", status="in_progress")], elapsed=601).state, "pending")

    def test_own_run_is_ignored(self):
        own = check("gate / aggregate", status="in_progress", cid=5, url=OWN_URL)
        self.assertEqual(verdict([own, check("a")]).state, "pass")

    def test_own_run_alone_proves_nothing(self):
        own = check("gate / aggregate", status="in_progress", cid=5, url=OWN_URL)
        self.assertEqual(verdict([own]).state, "fail")

    def test_ignore_patterns(self):
        v = verdict([check("a"), check("Grug - Guard", conclusion="neutral", cid=2)], ignore=["Grug - *"])
        self.assertEqual(v.state, "pass")

    def test_rerun_supersedes_older_attempt(self):
        old = check("a", conclusion="failure", cid=1)
        new = check("a", conclusion="success", cid=2)
        self.assertEqual(verdict([old, new]).state, "pass")
        self.assertEqual(verdict([new, old]).state, "pass")

    def test_same_name_in_different_suites_both_count(self):
        v = verdict([check("check", cid=1, suite=1), check("check", conclusion="failure", cid=2, suite=2)])
        self.assertEqual(v.state, "fail")

    def test_expected_missing_waits_then_fails(self):
        self.assertEqual(verdict([check("a")], expected=["b"], elapsed=10).state, "pending")
        v = verdict([check("a")], expected=["b"], elapsed=601)
        self.assertEqual(v.state, "fail")
        self.assertIn("never started", v.problems[0])

    def test_expected_glob(self):
        self.assertEqual(verdict([check("lint / ruff")], expected=["lint / *"]).state, "pass")

    def test_commit_statuses(self):
        self.assertEqual(verdict([check("a")], [{"context": "ext", "state": "success"}]).state, "pass")
        self.assertEqual(verdict([check("a")], [{"context": "ext", "state": "pending"}]).state, "pending")
        for s in ("failure", "error"):
            self.assertEqual(verdict([check("a")], [{"context": "ext", "state": s}]).state, "fail")

    def test_settle_holds_a_pass(self):
        self.assertEqual(verdict([check("a")], elapsed=5, settle=60).state, "pending")
        self.assertEqual(verdict([check("a")], elapsed=61, settle=60).state, "pass")

    def test_nothing_seen_fails(self):
        self.assertEqual(verdict([]).state, "fail")


class PollLoop(unittest.TestCase):
    def run_loop(self, snapshots, timeout=300, **over):
        clock = {"t": 0.0}
        seq = list(snapshots)

        def fetch():
            return seq.pop(0) if len(seq) > 1 else seq[0]

        def sleep(s):
            clock["t"] += s

        rules = {**RULES, **over}
        return poll(
            fetch,
            clock=lambda: clock["t"],
            sleep=sleep,
            interval=30,
            timeout=timeout,
            log=lambda m: None,
            **rules,
        ), clock["t"]

    def test_waits_for_the_others_then_passes(self):
        v, t = self.run_loop([
            Snapshot([check("a", status="queued")]),
            Snapshot([check("a", status="in_progress")]),
            Snapshot([check("a")]),
        ])
        self.assertEqual(v.state, "pass")
        self.assertEqual(t, 60)

    def test_still_running_at_timeout_fails(self):
        v, _ = self.run_loop([Snapshot([check("a", status="in_progress")])], timeout=120)
        self.assertEqual(v.state, "fail")
        self.assertIn("timeout", v.problems[0])

    def test_late_failure_fails(self):
        v, _ = self.run_loop([
            Snapshot([check("a", status="in_progress")]),
            Snapshot([check("a", conclusion="cancelled")]),
        ])
        self.assertEqual(v.problems, ["a: cancelled"])


class Cli(unittest.TestCase):
    def run_main(self, argv, env):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv, env)
        return code, out.getvalue()

    def test_fold_pass(self):
        code, _ = self.run_main(["fold"], {"NEEDS_JSON": json.dumps({"a": leg("success")})})
        self.assertEqual(code, EXIT_PASS)

    def test_fold_fail_annotates(self):
        code, out = self.run_main(["fold"], {"NEEDS_JSON": json.dumps({"a": leg("cancelled")})})
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("::error", out)

    def test_fold_env_lists(self):
        env = {
            "NEEDS_JSON": json.dumps({"a": leg("skipped")}),
            "GATE_ALLOW_SKIPPED": "a",
            "GATE_EXPECTED": "a, b",
        }
        code, out = self.run_main(["fold"], env)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("b: expected leg", out)

    def test_fold_bad_input_is_an_error(self):
        for raw in ("", "   ", "not json", "[1]"):
            with self.subTest(raw=raw):
                code, _ = self.run_main(["fold"], {"NEEDS_JSON": raw})
                self.assertEqual(code, EXIT_ERROR)

    def test_poll_needs_its_environment(self):
        code, out = self.run_main(["poll"], {})
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("GITHUB_TOKEN", out)


def jobs_block(text: str) -> dict[str, str]:
    """Split a workflow's `jobs:` section into {job id: job text} by indent."""
    body = text.split("\njobs:\n", 1)[1]
    jobs: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = ""
        elif current:
            jobs[current] += line + "\n"
    return jobs


def needs_of(job_text: str) -> list[str]:
    m = re.search(r"^    needs:\s*\[([^\]]*)\]", job_text, re.M)
    return [n.strip() for n in m.group(1).split(",")] if m else []


class WorkflowShape(unittest.TestCase):
    def test_reusable_passes_inputs_through_env_only(self):
        text = REUSABLE.read_text()
        self.assertIn("aggregate_gate.py", text)
        self.assertIn("NEEDS_JSON: ${{ inputs.needs-json }}", text)
        for block in re.findall(r"run: \|\n((?:          .*\n|\n)+)", text):
            self.assertNotIn("${{", block, "an input interpolated into run: text")

    def test_reusable_job_name_is_the_documented_check(self):
        self.assertIn("    name: aggregate\n", REUSABLE.read_text())

    def test_fold_callers_gate_every_leg_with_always(self):
        for path in (FOLD_CANARY, EXAMPLE):
            with self.subTest(path=path.name):
                jobs = jobs_block(path.read_text())
                gate = jobs["gate"]
                self.assertTrue(re.search(r"^    if: always\(\)$", gate, re.M), "gate needs if: always()")
                self.assertIn("_reusable.aggregate-gate.yml", gate)
                self.assertIn("needs-json: ${{ toJSON(needs) }}", gate)
                others = sorted(j for j in jobs if j != "gate")
                self.assertEqual(sorted(needs_of(gate)), others, "every leg must be in the gate's needs")

    def test_poll_canary_grants_read_scopes(self):
        text = POLL_CANARY.read_text()
        for scope in ("checks: read", "statuses: read"):
            self.assertIn(scope, text)
        self.assertIn("mode: poll", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
