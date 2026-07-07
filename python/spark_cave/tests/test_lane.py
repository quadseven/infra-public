"""Tests for the shared spark_cave enqueue lane.

Pins three things: the env-gating truth table for `build_lane_from_env`
(unset/0/1/garbage, case + whitespace insensitivity), that `enqueue` produces
a SendMessage whose FIFO MessageGroupId/MessageDeduplicationId match the live
consumers' behaviour, and that prefix/persona are real parameters (not
hardcoded to one persona) -- two differently-prefixed lanes don't cross-enable
each other.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[1]
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import pytest  # noqa: E402

from spark_cave.lane import CaveLane, build_lane_from_env, enqueue  # noqa: E402
from spark_cave.schema import FallbackJob  # noqa: E402


class _FakeSQS:
    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kw):
        self.sent.append(kw)


# ---- env-gating truth table (build_lane_from_env) --------------------------


@pytest.mark.parametrize("value", ["1", "true", "True", " TRUE ", "yes", "YES", "on", "On"])
def test_build_lane_enabled_for_every_truthy_spelling(monkeypatch, value):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", value)
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", "https://q/x.fifo")
    lane = build_lane_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQS)
    assert lane is not None
    assert lane.persona == "meal-gen"
    assert lane.jobs_queue_url == "https://q/x.fifo"


@pytest.mark.parametrize("value", [None, "0", "false", "off", "no", "garbage", "", "   "])
def test_build_lane_none_for_every_falsy_or_garbage_spelling(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MACCHINA_CAVE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MACCHINA_CAVE_ENABLED", value)
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", "https://q/x.fifo")
    assert build_lane_from_env("MACCHINA", persona="meal-gen") is None


def test_build_lane_none_when_queue_url_missing(monkeypatch):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.delenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", raising=False)
    assert build_lane_from_env("MACCHINA", persona="meal-gen") is None


def test_build_lane_none_when_queue_url_blank(monkeypatch):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", "   ")
    assert build_lane_from_env("MACCHINA", persona="meal-gen") is None


def test_build_lane_uses_injected_factory_not_boto3(monkeypatch):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", "https://q/macchina-jobs.fifo")
    sentinel = _FakeSQS()
    lane = build_lane_from_env("MACCHINA", persona="meal-gen", sqs_factory=lambda: sentinel)
    assert lane is not None
    assert lane.sqs is sentinel
    assert lane.jobs_queue_url == "https://q/macchina-jobs.fifo"


# ---- prefix/persona are real parameters, not hardcoded to one persona -----


def test_coach_prefix_env_vars_are_independent_of_meal_gen_prefix(monkeypatch):
    # Coach's own vars enable the coach lane...
    monkeypatch.setenv("MACCHINA_COACH_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_COACH_JOBS_QUEUE_URL", "https://q/coach.fifo")
    # ...while meal-gen's vars are unset -- the two lanes must not cross-enable.
    monkeypatch.delenv("MACCHINA_CAVE_ENABLED", raising=False)
    monkeypatch.delenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", raising=False)

    coach_lane = build_lane_from_env("MACCHINA_COACH", persona="coach", sqs_factory=_FakeSQS)
    meal_lane = build_lane_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQS)

    assert coach_lane is not None
    assert coach_lane.persona == "coach"
    assert coach_lane.jobs_queue_url == "https://q/coach.fifo"
    assert meal_lane is None


def test_throwaway_third_persona_needs_no_new_plumbing(monkeypatch):
    # A hypothetical future persona: only a prefix + persona string, no new
    # code -- proves the abstraction generalizes past the two personas that
    # exist today (the package's deepening goal).
    monkeypatch.setenv("SLEEP_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_SLEEP_JOBS_QUEUE_URL", "https://q/sleep.fifo")
    lane = build_lane_from_env("SLEEP", persona="sleep", sqs_factory=_FakeSQS)
    assert lane is not None
    assert lane.persona == "sleep"


# ---- enqueue: FIFO group/dedup parity with cave_tail's mirror --------------


def test_garbage_enabled_value_warns_but_disables(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "treu")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", "https://q/x.fifo")
    with caplog.at_level(logging.WARNING):
        lane = build_lane_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQS)
    assert lane is None
    assert any("not a recognized boolean" in r.message for r in caplog.records)


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off"])
def test_explicit_falsy_disables_silently(monkeypatch, caplog, falsy):
    import logging

    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", falsy)
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", "https://q/x.fifo")
    with caplog.at_level(logging.WARNING):
        lane = build_lane_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQS)
    assert lane is None
    assert not caplog.records


def test_enabled_without_boto3_or_factory_raises_actionable_import_error(monkeypatch):
    import builtins

    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL", "https://q/x.fifo")
    real_import = builtins.__import__

    def _no_boto3(name, *a, **kw):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_boto3)
    with pytest.raises(ImportError, match="install boto3 or"):
        build_lane_from_env("MACCHINA", persona="meal-gen")


@pytest.mark.asyncio
async def test_enqueue_sends_fifo_group_and_dedup_matching_cave_tail_shape():
    sqs = _FakeSQS()
    lane = CaveLane(sqs=sqs, jobs_queue_url="https://q/macchina-jobs.fifo", persona="meal-gen")

    job = await enqueue(
        lane,
        principal_id="user-123",
        request_id="req-1",
        payload={"prompt": "hi"},
    )

    assert isinstance(job, FallbackJob)
    assert len(sqs.sent) == 1
    msg = sqs.sent[0]
    assert msg["QueueUrl"] == "https://q/macchina-jobs.fifo"
    # FIFO group keeps one user's backlog isolated; dedup ties to this request
    # -- byte-identical shape to cave_tail.enqueue_meal_job's SendMessage.
    assert msg["MessageGroupId"] == "meal-gen:user-123"
    assert msg["MessageDeduplicationId"] == "meal-gen:user-123:req-1"
    body = FallbackJob.from_json(msg["MessageBody"])
    assert body.persona == "meal-gen"
    assert body.principal_id == "user-123"
    assert body.request_id == "req-1"
    assert body.payload.kind == "inline"
    assert body.payload.inline == {"prompt": "hi"}


@pytest.mark.asyncio
async def test_enqueue_coach_persona_group_and_dedup():
    sqs = _FakeSQS()
    lane = CaveLane(sqs=sqs, jobs_queue_url="https://q/coach.fifo", persona="coach")

    await enqueue(lane, principal_id="u", request_id="r", payload={"messages": []})

    msg = sqs.sent[0]
    assert msg["MessageGroupId"] == "coach:u"
    assert msg["MessageDeduplicationId"] == "coach:u:r"


@pytest.mark.asyncio
async def test_enqueue_unique_request_id_per_call_is_the_caller_s_job():
    # lane.enqueue does not generate request_id itself (cave_tail /
    # coach_cave_tail keep doing that in this slice); passing the same
    # request_id twice is a caller bug, not something the lane hides.
    sqs = _FakeSQS()
    lane = CaveLane(sqs=sqs, jobs_queue_url="https://q/x.fifo", persona="meal-gen")
    await enqueue(lane, principal_id="u", request_id="same", payload={"a": 1})
    await enqueue(lane, principal_id="u", request_id="same", payload={"a": 2})
    assert sqs.sent[0]["MessageDeduplicationId"] == sqs.sent[1]["MessageDeduplicationId"]


@pytest.mark.asyncio
async def test_enqueue_large_payload_spills_to_s3_when_putter_given():
    sqs = _FakeSQS()
    lane = CaveLane(sqs=sqs, jobs_queue_url="https://q/x.fifo", persona="meal-gen")
    puts: list[bytes] = []

    job = await enqueue(
        lane,
        principal_id="u",
        request_id="r",
        payload={"big": "y" * 1000},
        put_s3=lambda b: puts.append(b) or {"bucket": "B", "key": "K"},
        inline_threshold_bytes=50,
    )

    assert puts
    assert job.payload.kind == "s3"
    assert job.payload.s3 == {"bucket": "B", "key": "K"}


# ---- error behavior ---------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_oversized_payload_without_putter_raises_and_sends_nothing():
    sqs = _FakeSQS()
    lane = CaveLane(sqs=sqs, jobs_queue_url="https://q/x.fifo", persona="meal-gen")
    with pytest.raises(ValueError):
        await enqueue(
            lane,
            principal_id="u",
            request_id="r",
            payload={"big": "y" * 1000},
            inline_threshold_bytes=10,
        )
    assert sqs.sent == []
