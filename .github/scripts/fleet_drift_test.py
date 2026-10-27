#!/usr/bin/env python3
"""Fixture suite for fleet_drift.py (infra-public#167).

`fixtures/fleet_drift/` holds four small repos (`example__alpha` and so
on). alpha, beta and delta agree; gamma is behind on checkout, ruff and the
setup-uv `version:` input, ahead on python-version, and on `ubuntu-latest`
where the rest pin `ubuntu-24.04`. Only alpha and delta carry actionlint,
at different versions: the two-repo case.

Python stdlib only (unittest), matching the script.

Run: python3 .github/scripts/fleet_drift_test.py
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet_drift import (  # noqa: E402
    GitHub,
    analyse,
    build_plan,
    compare,
    extract,
    fetch,
    file_issues,
    main,
    repo_files,
    runner_compare,
    scan,
    settings,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fleet_drift"
CHECKOUT_V6 = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"
CHECKOUT_V5 = "93cb6efe18208431cddfb8368fd83d5badbf9bfd"


def plan_for_fixtures():
    return build_plan(FIXTURES)


def entries_for(repo):
    _, occs = scan(FIXTURES)
    entries, ahead = analyse(settings(occs))
    return entries.get(repo, []), ahead


class Versions(unittest.TestCase):
    def test_numeric_order(self):
        self.assertEqual(compare("v5.0.1", "v6.0.2"), -1)
        self.assertEqual(compare("0.15.10", "0.9.5"), 1)
        self.assertEqual(compare("3.12", "3.12"), 0)

    def test_floating_tag_and_release_do_not_order(self):
        self.assertIsNone(compare("v5", "v5.0.0"))
        self.assertIsNone(compare(CHECKOUT_V5, CHECKOUT_V6))

    def test_runner_latest_does_not_order(self):
        self.assertIsNone(runner_compare("ubuntu-latest", "ubuntu-24.04"))
        self.assertEqual(runner_compare("ubuntu-22.04", "ubuntu-24.04"), -1)


class Extraction(unittest.TestCase):
    def test_every_setting_kind_is_read_with_its_line(self):
        text = (FIXTURES / "example__alpha/.github/workflows/ci.yml").read_text()
        got = {
            (o.key, o.value, o.line) for o in extract("example/alpha", "ci.yml", text)
        }
        self.assertEqual(
            got,
            {
                ("runner:ubuntu", "ubuntu-24.04", 5),
                ("action:actions/checkout", "v6.0.2", 8),
                ("action:actions/setup-python", "v6.3.0", 9),
                ("python-version", "3.12", 11),
                ("action:astral-sh/setup-uv", "v8.0.0", 12),
                ("with:astral-sh/setup-uv:version", "0.9.5", 14),
                ("tool:ruff", "0.15.10", 15),
                ("tool:actionlint", "1.7.8", 16),
            },
        )

    def test_local_actions_expressions_and_comments_are_skipped(self):
        text = (
            "jobs:\n"
            "  a:\n"
            "    runs-on: ${{ matrix.os }}\n"
            "    steps:\n"
            "      # - uses: actions/checkout@v1\n"
            "      - uses: ./.github/actions/setup\n"
            "      - uses: actions/setup-python@v6\n"
            "        with:\n"
            "          python-version: ${{ matrix.py }}\n"
            "      - name: unrelated\n"
            "        with:\n"
            "          version: 9.9.9\n"
        )
        got = [(o.key, o.value) for o in extract("r/x", "w.yml", text)]
        self.assertEqual(got, [("action:actions/setup-python", "v6")])

    def test_composite_action_files_are_read(self):
        _, occs = scan(FIXTURES)
        paths = {o.path for o in occs if o.repo == "example/beta"}
        self.assertIn(".github/actions/setup/action.yml", paths)


class Majority(unittest.TestCase):
    def test_report_lists_every_repo_value_and_the_majority_with_file_line(self):
        _, summary = plan_for_fixtures()
        row = next(
            line
            for line in summary.splitlines()
            if line.startswith("| `action:actions/checkout`")
        )
        self.assertIn("| `v6.0.2` |", row)
        for repo, value, where in [
            ("example/alpha", "v6.0.2", ".github/workflows/ci.yml:8"),
            ("example/beta", "v6.0.2", ".github/workflows/ci.yml:8"),
            ("example/delta", "v6.0.2", ".github/workflows/lint.yml:8"),
            ("example/gamma", "v5.0.1", ".github/workflows/ci.yml:8"),
        ]:
            self.assertIn(f"{repo}: `{value}` ({where})", row)

    def test_behind_repo_gets_one_issue_with_every_difference_and_the_exact_change(
        self,
    ):
        plan, _ = plan_for_fixtures()
        gamma = [i for i in plan["issues"] if i["repo"] == "example/gamma"]
        self.assertEqual(len(gamma), 1)
        body = gamma[0]["body"]
        self.assertIn(
            f"- [ ] `.github/workflows/ci.yml:8` `action:actions/checkout`: "
            f"`- uses: actions/checkout@{CHECKOUT_V5} # v5.0.1` -> "
            f"`- uses: actions/checkout@{CHECKOUT_V6} # v6.0.2`",
            body,
        )
        self.assertIn(
            "`- run: uv tool run ruff==0.14.0 check .` -> `- run: uv tool run ruff==0.15.10 check .`",
            body,
        )
        self.assertIn('`version: "0.8.0"` -> `version: "0.9.5"`', body)

    def test_newer_pin_is_never_asked_to_downgrade(self):
        entries, ahead = entries_for("example/gamma")
        self.assertNotIn("python-version", {e.occ.key for e in entries})
        self.assertIn(("python-version", "3.13"), {(s.key, o.value) for s, o in ahead})
        plan, summary = plan_for_fixtures()
        body = next(i["body"] for i in plan["issues"] if i["repo"] == "example/gamma")
        self.assertNotIn("python-version", body)
        self.assertIn(
            "ahead of the majority `3.12` with `3.13`; not asked to downgrade", summary
        )
        # The repos on the majority are not filed against for being behind gamma.
        self.assertEqual(plan["clean"], ["example/alpha", "example/beta"])

    def test_a_tie_never_suggests_the_older_value_to_the_newer_repo(self):
        texts = {
            "a": "      - run: uvx ruff@0.15.0 check .\n",
            "b": "      - run: uvx ruff@0.15.0 check .\n",
            "c": "      - run: uvx ruff@0.16.0 check .\n",
            "d": "      - run: uvx ruff@0.16.0 check .\n",
        }
        occs = [o for r, t in texts.items() for o in extract(f"x/{r}", "w.yml", t)]
        entries, _ = analyse(settings(occs))
        self.assertEqual(sorted(entries), ["x/a", "x/b"])
        self.assertTrue(
            all(e.kind == "judgment" and e.target == "0.16.0" for e in entries["x/a"])
        )


class JudgmentCalls(unittest.TestCase):
    def test_two_repo_difference_is_a_judgment_call_not_a_defect(self):
        entries, _ = entries_for("example/delta")
        self.assertEqual(
            [(e.kind, e.occ.key, e.target) for e in entries],
            [("judgment", "tool:actionlint", "1.7.8")],
        )
        self.assertIn("only two repos carry it", entries[0].change)
        alpha, _ = entries_for("example/alpha")
        self.assertEqual(alpha, [])
        plan, _ = plan_for_fixtures()
        body = next(i["body"] for i in plan["issues"] if i["repo"] == "example/delta")
        self.assertIn("## Judgment calls", body)
        self.assertNotIn("## Behind the majority", body)

    def test_values_that_do_not_order_are_judgment_calls(self):
        entries, _ = entries_for("example/gamma")
        runner = [e for e in entries if e.occ.key == "runner:ubuntu"]
        self.assertEqual([e.kind for e in runner], ["judgment"])


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeAPI:
    """Records every request; answers from a table keyed by (method, path)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, req, timeout):
        path = req.full_url.replace("https://api.github.com", "")
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body, timeout))
        ans = self.answers.get((req.get_method(), path.split("?")[0]))
        if isinstance(ans, Exception):
            raise ans
        return FakeResponse(json.dumps(ans).encode() if ans is not None else b"")


class Filing(unittest.TestCase):
    PLAN = {
        "repos": ["o/a", "o/b", "o/c"],
        "issues": [
            {"repo": "o/a", "title": "Fleet drift: o/a", "body": "A"},
            {"repo": "o/b", "title": "Fleet drift: o/b", "body": "B"},
        ],
        "clean": ["o/c"],
    }
    OPEN = [
        {"number": 7, "title": "Fleet drift: o/a"},
        {"number": 9, "title": "Fleet drift: o/c"},
        {"number": 11, "title": "Fleet drift: o/z", "pull_request": {}},
    ]

    def api(self):
        exists = urllib.error.HTTPError("u", 422, "exists", {}, None)
        return FakeAPI(
            {
                ("GET", "/repos/t/r/issues"): self.OPEN,
                ("POST", "/repos/t/r/labels"): exists,
            }
        )

    def test_updates_existing_creates_missing_and_closes_clean(self):
        fake = self.api()
        with contextlib.redirect_stdout(io.StringIO()):
            file_issues(GitHub("tok", fake), self.PLAN, "t/r", dry_run=False)
        writes = [(m, p, b) for m, p, b, _ in fake.calls if m != "GET"]
        self.assertIn(("PATCH", "/repos/t/r/issues/7", {"body": "A"}), writes)
        self.assertIn(
            (
                "POST",
                "/repos/t/r/issues",
                {"title": "Fleet drift: o/b", "body": "B", "labels": ["fleet-drift"]},
            ),
            writes,
        )
        self.assertIn(
            (
                "PATCH",
                "/repos/t/r/issues/9",
                {"state": "closed", "state_reason": "completed"},
            ),
            writes,
        )
        self.assertTrue(all(t == 30 for *_, t in fake.calls))

    def test_dry_run_writes_nothing(self):
        fake = self.api()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            actions = file_issues(GitHub("tok", fake), self.PLAN, "t/r", dry_run=True)
        self.assertEqual({m for m, *_ in fake.calls}, {"GET"})
        self.assertEqual(
            actions,
            [
                "update #7 Fleet drift: o/a",
                "create Fleet drift: o/b",
                "close #9 Fleet drift: o/c",
            ],
        )


class Client(unittest.TestCase):
    def test_only_api_paths_are_opened(self):
        fake = FakeAPI({})
        for bad in ("file:///etc/passwd", "https://example.com/x", "repos/o/a"):
            with self.assertRaises(ValueError):
                GitHub("tok", fake).call("GET", bad)
        self.assertEqual(fake.calls, [])


class Fetching(unittest.TestCase):
    def test_fetch_copies_only_workflow_and_action_files_read_only(self):
        content = base64.b64encode(b"name: ci\n").decode()
        fake = FakeAPI(
            {
                ("GET", "/repos/o/a"): {"default_branch": "main"},
                ("GET", "/repos/o/a/git/trees/main"): {
                    "tree": [
                        {
                            "type": "blob",
                            "path": ".github/workflows/ci.yml",
                            "sha": "s1",
                        },
                        {
                            "type": "blob",
                            "path": ".github/actions/setup/action.yml",
                            "sha": "s2",
                        },
                        {"type": "blob", "path": "src/app.py", "sha": "s3"},
                        {
                            "type": "blob",
                            "path": ".github/workflows/nested/x.yml",
                            "sha": "s4",
                        },
                    ]
                },
                ("GET", "/repos/o/a/git/blobs/s1"): {"content": content},
                ("GET", "/repos/o/a/git/blobs/s2"): {"content": content},
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            fetch(GitHub("tok", fake), ["o/a"], Path(tmp))
            got = sorted(
                p.relative_to(tmp).as_posix()
                for p in Path(tmp).rglob("*")
                if p.is_file()
            )
        self.assertEqual(
            got,
            ["o__a/.github/actions/setup/action.yml", "o__a/.github/workflows/ci.yml"],
        )
        self.assertEqual({m for m, *_ in fake.calls}, {"GET"})


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


class Workflows(unittest.TestCase):
    def test_reusable_runs_fetch_report_then_file_and_honours_dry_run(self):
        text = (WORKFLOWS / "_reusable.fleet-drift.yml").read_text()
        order = [text.index(f"fleet_drift.py {cmd}") for cmd in ("fetch", "report")]
        order.append(text.index('"${args[@]}"'))
        self.assertEqual(order, sorted(order))
        self.assertIn("args=(file --plan", text)
        self.assertIn(
            'if [ "$DRY_RUN" = "true" ]; then\n            args+=(--dry-run)', text
        )
        self.assertIn("GH_TOKEN: ${{ secrets.token || github.token }}", text)

    def test_self_run_is_a_dry_run_on_pull_requests(self):
        text = (WORKFLOWS / "self-fleet-drift.yml").read_text()
        self.assertIn("uses: ./.github/workflows/_reusable.fleet-drift.yml", text)
        self.assertIn("dry-run: ${{ github.event_name == 'pull_request' }}", text)
        self.assertIn('cron: "0 13 * * 1"', text)


class Cli(unittest.TestCase):
    def test_report_then_dry_run_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, summary = Path(tmp) / "plan.json", Path(tmp) / "summary.md"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "report",
                            "--root",
                            str(FIXTURES),
                            "--plan",
                            str(plan),
                            "--summary",
                            str(summary),
                        ]
                    ),
                    0,
                )
            data = json.loads(plan.read_text())
            self.assertEqual(
                [i["title"] for i in data["issues"]],
                ["Fleet drift: example/delta", "Fleet drift: example/gamma"],
            )
            self.assertTrue(summary.read_text().isascii())
            fake = FakeAPI({("GET", "/repos/t/r/issues"): []})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(
                    ["file", "--plan", str(plan), "--issue-repo", "t/r", "--dry-run"],
                    opener=fake,
                )
            self.assertIn(
                "dry run: would create Fleet drift: example/gamma", out.getvalue()
            )
            self.assertEqual({m for m, *_ in fake.calls}, {"GET"})

    def test_bad_repo_name_is_refused(self):
        with self.assertRaises(SystemExit):
            main(["fetch", "--repos", "not-a-repo", "--out", "x"])


REPO_ROOT = Path(__file__).resolve().parents[2]


class OwnPins(unittest.TestCase):
    """This repo's own workflows sit at the fleet majority (infra-public#174).

    The weekly report filed every `actions/checkout` pin here as behind the
    fleet at v5.0.1 while the fleet ran v6.0.2. Read the pins the same way
    the report does, so a pin that slides back is caught in CI, not a week
    later in a drift issue.
    """

    def test_checkout_is_at_least_the_fleet_majority(self):
        files = repo_files(REPO_ROOT)
        self.assertTrue(files, "no workflow files found under the repo root")
        behind = []
        for f in files:
            rel = f.relative_to(REPO_ROOT).as_posix()
            for occ in extract("self", rel, f.read_text(encoding="utf-8")):
                if occ.key != "action:actions/checkout":
                    continue
                if occ.ref != CHECKOUT_V6 and compare(occ.value, "v6.0.2") != 1:
                    behind.append(f"{rel}:{occ.line} {occ.value}")
        self.assertEqual(behind, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
