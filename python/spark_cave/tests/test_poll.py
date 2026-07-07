"""Tests for the shared spark_cave result-drain channel.

Pins: the env-gating truth table for `build_result_channel_from_env` (mirrors
`lane`'s), that `fetch_result_for` matches a FIFO message by persona +
request_id and leaves every other message alone, that it deletes ONLY the
matched message, that an empty/hiccuping receive is honest `pending` and
never raises, that an `ok=false` result is honest `failed` with a bounded
`detail`, that a `parse` failure (raise OR empty/falsy return) is honest
`failed` with no `detail`, that the batch scan is bounded (a deep queue of
non-matching messages cannot make one call run unbounded), that an abandoned
non-matching message gets purged while a fresh one is left alone, and that S3
spillover unwrap works. A final round-trip test drives a throwaway persona
through `lane.enqueue` -> `poll.fetch_result_for` using only the shared
primitives, proving a new persona needs no new plumbing on either side.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[1]
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import pytest  # noqa: E402

from spark_cave.lane import CaveLane, enqueue  # noqa: E402
from spark_cave.poll import (  # noqa: E402
    ABANDONED_RESULT_MAX_AGE_SECONDS,
    MAX_RECEIVE_BATCHES,
    RECEIVE_BATCH_SIZE,
    CaveOutcome,
    CaveResultChannel,
    build_result_channel_from_env,
    fetch_result_for,
)
from spark_cave.schema import SCHEMA_VERSION, FallbackResult  # noqa: E402


class _FakeSQSReceiver:
    """A minimal in-memory FIFO: `receive_message` returns up to
    `MaxNumberOfMessages` still-present messages in order; `delete_message`
    removes by ReceiptHandle. Tracks receive call count so tests can assert
    the batch scan is bounded."""

    def __init__(self):
        self._messages: list[dict] = []
        self.receive_calls = 0
        self._next_handle = 0

    def add(self, body: str, *, sent_ms: float | None = None) -> str:
        self._next_handle += 1
        handle = f"handle-{self._next_handle}"
        msg: dict = {"Body": body, "ReceiptHandle": handle}
        if sent_ms is not None:
            msg["Attributes"] = {"SentTimestamp": str(int(sent_ms))}
        self._messages.append(msg)
        return handle

    def receive_message(self, *, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds, AttributeNames=None):
        self.receive_calls += 1
        batch = self._messages[:MaxNumberOfMessages]
        return {"Messages": batch}

    def delete_message(self, *, QueueUrl, ReceiptHandle):
        self._messages = [m for m in self._messages if m.get("ReceiptHandle") != ReceiptHandle]

    @property
    def remaining_bodies(self) -> list[str]:
        return [m["Body"] for m in self._messages]


def _result(request_id: str, *, persona: str = "throwaway", ok: bool = True, result=None, error=None) -> str:
    return FallbackResult(
        schema_version=SCHEMA_VERSION,
        persona=persona,
        principal_id="u",
        request_id=request_id,
        ok=ok,
        result=result,
        error=error,
    ).to_json()


async def _echo_parse(payload: dict) -> dict:
    return payload


# ---- env-gating truth table (build_result_channel_from_env) ---------------


@pytest.mark.parametrize("value", ["1", "true", "True", " TRUE ", "yes", "on"])
def test_build_channel_enabled_for_every_truthy_spelling(monkeypatch, value):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", value)
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    channel = build_result_channel_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQSReceiver)
    assert channel is not None
    assert channel.persona == "meal-gen"
    assert channel.results_queue_url == "https://q/results.fifo"
    assert channel.get_s3 is None


@pytest.mark.parametrize("value", [None, "0", "false", "off", "no", "garbage", "", "   "])
def test_build_channel_none_for_every_falsy_or_garbage_spelling(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MACCHINA_CAVE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MACCHINA_CAVE_ENABLED", value)
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    assert build_result_channel_from_env("MACCHINA", persona="meal-gen") is None


def test_build_channel_none_when_queue_url_missing(monkeypatch):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.delenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", raising=False)
    assert build_result_channel_from_env("MACCHINA", persona="meal-gen") is None


def test_build_channel_none_when_queue_url_blank(monkeypatch):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "   ")
    assert build_result_channel_from_env("MACCHINA", persona="meal-gen") is None


def test_garbage_enabled_value_warns_but_disables(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "treu")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    with caplog.at_level(logging.WARNING):
        channel = build_result_channel_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQSReceiver)
    assert channel is None
    assert any("not a recognized boolean" in r.message for r in caplog.records)


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off"])
def test_explicit_falsy_disables_silently(monkeypatch, caplog, falsy):
    import logging

    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", falsy)
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    with caplog.at_level(logging.WARNING):
        channel = build_result_channel_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQSReceiver)
    assert channel is None
    assert not caplog.records


def test_enabled_without_boto3_or_factory_raises_actionable_import_error(monkeypatch):
    import builtins

    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    real_import = builtins.__import__

    def _no_boto3(name, *a, **kw):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_boto3)
    with pytest.raises(ImportError, match="install boto3 or"):
        build_result_channel_from_env("MACCHINA", persona="meal-gen")


def test_coach_prefix_env_vars_are_independent_of_meal_gen_prefix(monkeypatch):
    monkeypatch.setenv("MACCHINA_COACH_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_COACH_RESULTS_QUEUE_URL", "https://q/coach-results.fifo")
    monkeypatch.delenv("MACCHINA_CAVE_ENABLED", raising=False)
    monkeypatch.delenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", raising=False)

    coach = build_result_channel_from_env("MACCHINA_COACH", persona="coach", sqs_factory=_FakeSQSReceiver)
    meal = build_result_channel_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQSReceiver)

    assert coach is not None
    assert coach.persona == "coach"
    assert coach.results_queue_url == "https://q/coach-results.fifo"
    assert meal is None


def test_throwaway_third_persona_needs_no_new_plumbing(monkeypatch):
    # A hypothetical future persona: only a prefix + persona string, no new
    # code -- proves the abstraction generalizes past today's personas.
    monkeypatch.setenv("SLEEP_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_SLEEP_RESULTS_QUEUE_URL", "https://q/sleep-results.fifo")
    channel = build_result_channel_from_env("SLEEP", persona="sleep", sqs_factory=_FakeSQSReceiver)
    assert channel is not None
    assert channel.persona == "sleep"


def test_build_channel_wires_s3_getter_only_when_bucket_configured(monkeypatch):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_PAYLOAD_BUCKET", "macchina-cave-payloads")

    class _FakeS3:
        def get_object(self, *, Bucket, Key):
            assert Bucket == "macchina-cave-payloads"
            assert Key == "k1"

            class _Body:
                def read(self_inner) -> bytes:
                    return json.dumps({"hydrated": True}).encode("utf-8")

            return {"Body": _Body()}

    channel = build_result_channel_from_env(
        "MACCHINA",
        persona="meal-gen",
        sqs_factory=_FakeSQSReceiver,
        s3_factory=_FakeS3,
    )
    assert channel is not None
    assert channel.get_s3 is not None
    assert channel.get_s3({"bucket": "macchina-cave-payloads", "key": "k1"}) == json.dumps(
        {"hydrated": True}
    ).encode("utf-8")


# ---- fetch_result_for: matching + delete-only-the-match --------------------


@pytest.mark.asyncio
async def test_matched_result_among_others_returns_ready_and_deletes_only_that_one():
    sqs = _FakeSQSReceiver()
    other1 = sqs.add(_result("someone-elses-request", persona="throwaway"))
    mine = sqs.add(_result("my-request", persona="throwaway", result={"x": 1}))
    other2 = sqs.add(_result("yet-another-request", persona="throwaway"))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="my-request", parse=_echo_parse)

    assert outcome == CaveOutcome(status="ready", parsed={"x": 1})
    remaining = sqs.remaining_bodies
    assert len(remaining) == 2
    assert not any('"request_id": "my-request"' in b for b in remaining)
    del other1, other2, mine  # readability only; assertions above are the real check


@pytest.mark.asyncio
async def test_persona_mismatch_is_not_a_match():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("my-request", persona="other-persona"))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="my-request", parse=_echo_parse)

    assert outcome.status == "pending"


# ---- honest pending: empty + hiccuping receive -----------------------------


@pytest.mark.asyncio
async def test_empty_queue_returns_pending():
    sqs = _FakeSQSReceiver()
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")
    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)
    assert outcome == CaveOutcome(status="pending")


@pytest.mark.asyncio
async def test_receive_exception_returns_pending_and_never_raises():
    class _BoomSQS(_FakeSQSReceiver):
        def receive_message(self, **kwargs):
            raise RuntimeError("ReceiveMessage transport blip")

    channel = CaveResultChannel(sqs=_BoomSQS(), results_queue_url="https://q/x.fifo", persona="throwaway")
    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)
    assert outcome == CaveOutcome(status="pending")


# ---- failed: ok=false, and parse failure/garbled ---------------------------


@pytest.mark.asyncio
async def test_ok_false_result_is_failed_with_bounded_detail_and_is_deleted():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", ok=False, error="no_grounded_option"))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)

    assert outcome == CaveOutcome(status="failed", detail="no_grounded_option")
    assert sqs.remaining_bodies == []


@pytest.mark.asyncio
async def test_ok_false_result_with_non_whitelisted_error_has_no_detail():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", ok=False, error="whatever broke! (see logs)"))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)

    assert outcome.status == "failed"
    assert outcome.detail is None


@pytest.mark.asyncio
async def test_parse_raising_is_failed_with_no_detail_and_is_deleted():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", result={"garbled": True}))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    async def _boom(_payload: dict):
        raise ValueError("not a valid domain object")

    outcome = await fetch_result_for(channel, request_id="r", parse=_boom)

    assert outcome == CaveOutcome(status="failed")
    assert sqs.remaining_bodies == []


@pytest.mark.asyncio
async def test_parse_returning_empty_is_failed_not_ready():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", result={"candidates": []}))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    async def _parse_to_nothing(payload: dict) -> list:
        return payload["candidates"]  # empty list: everything filtered away

    outcome = await fetch_result_for(channel, request_id="r", parse=_parse_to_nothing)

    assert outcome == CaveOutcome(status="failed")
    assert sqs.remaining_bodies == []


@pytest.mark.asyncio
async def test_malformed_body_is_never_raised_and_never_deleted():
    sqs = _FakeSQSReceiver()
    sqs.add("not json at all")
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)

    assert outcome.status == "pending"
    assert sqs.remaining_bodies == ["not json at all"]


# ---- bounded batch scan -----------------------------------------------------


@pytest.mark.asyncio
async def test_deep_queue_of_non_matches_cannot_run_one_poll_unbounded():
    sqs = _FakeSQSReceiver()
    # Far more non-matching messages than one call could ever drain if the
    # scan were unbounded.
    for i in range(RECEIVE_BATCH_SIZE * (MAX_RECEIVE_BATCHES + 5)):
        sqs.add(_result(f"other-{i}", persona="throwaway"))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="never-there", parse=_echo_parse)

    assert outcome == CaveOutcome(status="pending")
    assert sqs.receive_calls == MAX_RECEIVE_BATCHES


# ---- abandoned-message purge ------------------------------------------------


@pytest.mark.asyncio
async def test_abandoned_non_matching_message_is_purged():
    sqs = _FakeSQSReceiver()
    stale_age = ABANDONED_RESULT_MAX_AGE_SECONDS + 120
    sqs.add(
        _result("someone-elses-old-request", persona="throwaway"), sent_ms=(time.time() - stale_age) * 1000
    )
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="not-this-one", parse=_echo_parse)

    assert outcome.status == "pending"
    assert sqs.remaining_bodies == []  # the abandoned message was purged


@pytest.mark.asyncio
async def test_fresh_non_matching_message_is_left_alone():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("someone-elses-fresh-request", persona="throwaway"), sent_ms=time.time() * 1000)
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="not-this-one", parse=_echo_parse)

    assert outcome.status == "pending"
    assert len(sqs.remaining_bodies) == 1  # left for its rightful poller


@pytest.mark.asyncio
async def test_non_matching_message_with_no_sent_timestamp_is_left_alone():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("someone-elses-request", persona="throwaway"))  # no sent_ms attribute at all
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="not-this-one", parse=_echo_parse)

    assert outcome.status == "pending"
    assert len(sqs.remaining_bodies) == 1


def test_build_channel_s3_getter_rejects_foreign_bucket(monkeypatch):
    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_PAYLOAD_BUCKET", "macchina-cave-payloads")

    class _FakeS3:
        calls = 0

        def get_object(self, **kw):
            _FakeS3.calls += 1
            raise AssertionError("must never be called for a foreign bucket")

    channel = build_result_channel_from_env(
        "MACCHINA", persona="meal-gen", sqs_factory=_FakeSQSReceiver, s3_factory=_FakeS3
    )
    assert channel is not None and channel.get_s3 is not None
    with pytest.raises(ValueError, match="configured for"):
        channel.get_s3({"bucket": "attacker-bucket", "key": "k1"})
    assert _FakeS3.calls == 0


def test_build_channel_s3_missing_boto3_raises_actionable_error(monkeypatch):
    import builtins

    monkeypatch.setenv("MACCHINA_CAVE_ENABLED", "1")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_RESULTS_QUEUE_URL", "https://q/results.fifo")
    monkeypatch.setenv("SPARK_CAVE_MACCHINA_PAYLOAD_BUCKET", "macchina-cave-payloads")
    real_import = builtins.__import__

    def _no_boto3(name, *a, **kw):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_boto3)
    with pytest.raises(ImportError, match="install boto3 or"):
        build_result_channel_from_env("MACCHINA", persona="meal-gen", sqs_factory=_FakeSQSReceiver)


# ---- S3 spillover unwrap -----------------------------------------------------


@pytest.mark.asyncio
async def test_sync_parse_callback_is_supported():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", result={"x": 1}))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    def sync_parse(payload: dict):
        return {"seen": payload["x"]}

    outcome = await fetch_result_for(channel, request_id="r", parse=sync_parse)
    assert outcome == CaveOutcome(status="ready", parsed={"seen": 1})


@pytest.mark.asyncio
async def test_unicode_alnum_error_detail_is_dropped():
    # str.isalnum() accepts unicode letters; the whitelist must not.
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", ok=False, error="\u00e9chec_r\u00e9seau"))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")
    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)
    assert outcome.status == "failed"
    assert outcome.detail is None


@pytest.mark.asyncio
async def test_s3_spillover_result_is_unwrapped_before_parse():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", result={"kind": "s3", "s3": {"bucket": "b", "key": "k"}}))

    def get_s3(pointer: dict) -> bytes:
        assert pointer == {"bucket": "b", "key": "k"}
        return json.dumps({"hydrated": "yes"}).encode("utf-8")

    channel = CaveResultChannel(
        sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway", get_s3=get_s3
    )

    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)

    assert outcome == CaveOutcome(status="ready", parsed={"hydrated": "yes"})


@pytest.mark.asyncio
async def test_s3_spillover_without_getter_configured_is_failed_not_a_crash():
    sqs = _FakeSQSReceiver()
    sqs.add(_result("r", persona="throwaway", result={"kind": "s3", "s3": {"bucket": "b", "key": "k"}}))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)

    assert outcome == CaveOutcome(status="failed")


# ---- delete failure never bubbles -------------------------------------------


@pytest.mark.asyncio
async def test_delete_failure_on_a_matched_result_still_returns_the_outcome():
    class _DeleteBoomSQS(_FakeSQSReceiver):
        def delete_message(self, **kwargs):
            raise RuntimeError("DeleteMessage transport blip")

    sqs = _DeleteBoomSQS()
    sqs.add(_result("r", persona="throwaway", result={"x": 1}))
    channel = CaveResultChannel(sqs=sqs, results_queue_url="https://q/x.fifo", persona="throwaway")

    outcome = await fetch_result_for(channel, request_id="r", parse=_echo_parse)

    assert outcome == CaveOutcome(status="ready", parsed={"x": 1})


# ---- full round trip: a throwaway persona needs no new plumbing -----------


@pytest.mark.asyncio
async def test_throwaway_persona_enqueue_then_drain_round_trip_via_shared_modules_only():
    """Binds a brand-new persona with only a trivial codec + parser + prefix,
    and drives one enqueue -> drain round trip through `lane` + `poll` alone
    -- proving a future persona is config, not a new plumbing fork."""

    class _FakeEnqueueSQS:
        def __init__(self):
            self.sent: list[dict] = []

        def send_message(self, **kw):
            self.sent.append(kw)

    enqueue_sqs = _FakeEnqueueSQS()
    lane = CaveLane(sqs=enqueue_sqs, jobs_queue_url="https://q/sleep-jobs.fifo", persona="sleep")
    job = await enqueue(lane, principal_id="u1", request_id="req-1", payload={"hours": 8})
    assert job.dedup_key == "sleep:u1:req-1"

    # Simulate the on-prem connector publishing a result for that request.
    results_sqs = _FakeSQSReceiver()
    results_sqs.add(_result("req-1", persona="sleep", result={"hours": 8, "quality": "good"}))
    channel = CaveResultChannel(
        sqs=results_sqs, results_queue_url="https://q/sleep-results.fifo", persona="sleep"
    )

    async def parse_sleep_result(payload: dict) -> str:
        return f"slept {payload['hours']}h, quality={payload['quality']}"

    outcome = await fetch_result_for(channel, request_id="req-1", parse=parse_sleep_result)

    assert outcome == CaveOutcome(status="ready", parsed="slept 8h, quality=good")
    assert results_sqs.remaining_bodies == []
