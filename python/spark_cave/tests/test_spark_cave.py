"""Tests for the spark_cave shared airlock lib: schema round-trip + dedup, and
the result handler's dispatch/idempotency/never-raise guarantees."""

from __future__ import annotations

import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[1]
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import pytest  # noqa: E402

from spark_cave.enqueue import enqueue  # noqa: E402
from spark_cave.packing import pack, unpack  # noqa: E402
from spark_cave.results import handle_result  # noqa: E402
from spark_cave.schema import (  # noqa: E402
    SCHEMA_VERSION,
    FallbackJob,
    FallbackResult,
    PayloadRef,
)


class _FakeSQS:
    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kw):
        self.sent.append(kw)


def _job() -> FallbackJob:
    return FallbackJob(SCHEMA_VERSION, "meal-gen", "u1", "r1", PayloadRef("inline", {"a": 1}, None))


def test_job_round_trips_inline_payload():
    j = _job()
    back = FallbackJob.from_json(j.to_json())
    assert back == j and back.payload.inline == {"a": 1}


def test_job_round_trips_s3_payload():
    j = FallbackJob(
        SCHEMA_VERSION,
        "code-review",
        "u2",
        "r2",
        PayloadRef("s3", None, {"bucket": "b", "key": "k"}),
    )
    assert FallbackJob.from_json(j.to_json()) == j


def test_dedup_key_is_persona_principal_request():
    assert _job().dedup_key == "meal-gen:u1:r1"


def test_result_round_trips():
    r = FallbackResult(SCHEMA_VERSION, "meal-gen", "u1", "r1", True, {"proposals": []}, None)
    assert FallbackResult.from_json(r.to_json()) == r


def test_handle_result_dispatches_to_the_right_persona():
    seen: list[str] = []
    r = FallbackResult(SCHEMA_VERSION, "meal-gen", "u1", "r1", True, {"x": 1}, None)
    ran = handle_result(r.to_json(), handlers={"meal-gen": lambda res: seen.append(res.request_id)})
    assert ran is True and seen == ["r1"]


def test_handle_result_unknown_persona_is_noop():
    r = FallbackResult(SCHEMA_VERSION, "other", "u", "r", True, None, None)
    assert handle_result(r.to_json(), handlers={}) is False


def test_handle_result_never_raises_on_bad_body():
    assert handle_result("not json", handlers={}) is False


def test_handle_result_swallows_handler_exception():
    def boom(_res):
        raise RuntimeError("downstream blew up")

    r = FallbackResult(SCHEMA_VERSION, "meal-gen", "u", "r", False, None, "err")
    assert handle_result(r.to_json(), handlers={"meal-gen": boom}) is False


# ---- packing (inline vs S3 spillover) --------------------------------------


def test_pack_small_payload_is_inline():
    ref = pack({"a": 1}, threshold_bytes=1000)
    assert ref.kind == "inline" and ref.inline == {"a": 1} and ref.s3 is None


def test_pack_large_payload_spills_to_s3():
    captured: dict = {}

    def put(b: bytes) -> dict:
        captured["bytes"] = b
        return {"bucket": "B", "key": "K"}

    ref = pack({"big": "x" * 500}, threshold_bytes=50, put_s3=put)
    assert ref.kind == "s3" and ref.s3 == {"bucket": "B", "key": "K"} and ref.inline is None
    assert b"big" in captured["bytes"]


def test_pack_large_without_putter_raises():
    with pytest.raises(ValueError):
        pack({"big": "x" * 500}, threshold_bytes=10)


def test_unpack_inline_returns_payload():
    assert unpack(PayloadRef("inline", {"a": 1}, None)) == {"a": 1}


def test_unpack_s3_uses_getter():
    import json

    ref = PayloadRef("s3", None, {"bucket": "B", "key": "K"})
    assert unpack(ref, get_s3=lambda s3: json.dumps({"x": 9}).encode()) == {"x": 9}


def test_unpack_unknown_kind_raises():
    with pytest.raises(ValueError):
        unpack(PayloadRef("weird", None, None))


# ---- enqueue (FIFO group + dedup, S3 spillover) ----------------------------


def test_enqueue_sends_to_fifo_with_group_and_dedup():
    sqs = _FakeSQS()
    job = enqueue(
        sqs=sqs,
        queue_url="Q",
        persona="meal-gen",
        principal_id="u1",
        request_id="r1",
        payload={"prompt": "hi"},
    )
    assert len(sqs.sent) == 1
    msg = sqs.sent[0]
    assert msg["QueueUrl"] == "Q"
    assert msg["MessageGroupId"] == "meal-gen:u1"
    assert msg["MessageDeduplicationId"] == "meal-gen:u1:r1"
    body = FallbackJob.from_json(msg["MessageBody"])
    assert body.persona == "meal-gen" and body.payload.inline == {"prompt": "hi"}
    assert job.request_id == "r1"


def test_enqueue_large_payload_spills_to_s3():
    sqs = _FakeSQS()
    puts: list[bytes] = []
    enqueue(
        sqs=sqs,
        queue_url="Q",
        persona="meal-gen",
        principal_id="u",
        request_id="r",
        payload={"big": "y" * 1000},
        put_s3=lambda b: puts.append(b) or {"bucket": "B", "key": "K"},
        inline_threshold_bytes=50,
    )
    assert puts
    body = FallbackJob.from_json(sqs.sent[0]["MessageBody"])
    assert body.payload.kind == "s3" and body.payload.s3 == {"bucket": "B", "key": "K"}
