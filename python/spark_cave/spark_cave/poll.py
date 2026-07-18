"""Env-gated Spark-Cave result-drain loop, shared across personas.

Hoists the RESULT-side (drain) half of the request lifecycle that each
consuming persona otherwise defines independently: an env-gated
`CaveResultChannel` (results-queue URL + injected sync SQS receiver +
optional S3 getter) built once at startup, and `fetch_result_for` -- the
bounded batch-scan + long-poll + match-by-`request_id` + delete-on-match loop
that turns a matched result into the caller's honest `CaveOutcome`.

Additive: importing this module changes no call site and no behaviour of the
other submodules. Sibling of `lane` (the enqueue-side counterpart); together
they cover the full request -> drain lifecycle behind small, persona-agnostic
primitives.

DECISIONS -- read before touching `fetch_result_for` or its factory. Every
consuming persona's result-drain implementation converged on the same shape,
but two details drifted between them; this module encodes ONE answer for
each rather than silently preferring one persona's copy:

1. Env-var gating reuses `lane`'s hardened truthy/falsy parsing (warn once on
   an unrecognized `<PREFIX>_CAVE_ENABLED` value, stay quiet on an explicit
   falsy) rather than a plainer `not in {truthy set}` check. This is the
   SAME flag `lane.build_lane_from_env` already gates on for the enqueue
   side of the same persona, so the two should read it identically -- one of
   today's consumers still has the plainer, unhardened check on its own
   local copy of this exact flag; this module carries the newer, hardened
   reading forward instead of reproducing the older one.
2. One consuming persona's drain loop learned (the hard way, via a live
   incident) that a stale, never-claimable result left sitting on a FIFO
   queue blocks that queue's head-of-line for every subsequent match -- so
   it added a defensive purge of any non-matching message older than
   `ABANDONED_RESULT_MAX_AGE_SECONDS`. A second, newer persona's drain loop
   predates that fix and never picked it up. This module makes the purge
   universal (every channel gets it for free) rather than leaving it as a
   fix one persona has and another is quietly missing.

Two further interface choices worth flagging even though both consumers
agree on them today (so they are not "disagreements", just decisions):

3. `parse`'s parameter is the unwrapped RESULT PAYLOAD (a `dict`), matching
   every persona's own validator signature (`dict -> domain object`) exactly
   -- not the raw `FallbackResult` wire envelope. The envelope-level
   concerns that are identical across every persona (`ok=false` -> honest
   `failed`, and unwrapping a spilled-to-S3 payload) are owned HERE, once,
   so a persona's `parse` only ever sees "my domain payload, already
   resolved to a plain dict, guaranteed successful at the wire level."
4. `parse` is called under ONE try/except that covers both validating the
   payload AND any further domain-specific transform (e.g. re-ranking) the
   persona wants to run on it, and an empty/falsy return from `parse` is
   treated the same as a raised exception (both -> honest `failed`, no
   detail). Today's strictest consumer's own copy only wrapped the
   validation half in a try/except, leaving a narrow gap where a downstream
   transform's exception was not caught; wrapping the whole callable closes
   that gap without changing observable behaviour for a `parse` that never
   raises past validation.

Also: the results-side SQS client factory below builds an explicit
`Session().client(...)` rather than the bare module-level `boto3.client(...)`
convenience `lane`'s enqueue side uses -- a poll loop is invoked from many
concurrent request-handling threads (one per in-flight poll), which is
exactly the concurrent-construction case AWS's guidance warns the shared
default session is not safe for; the enqueue side's single send is a much
narrower window that has not shown the issue in practice, but the read side
takes the more defensive form since it is the one actually exercised
concurrently today.

Operational assumption: ONE logical drain consumer per persona/queue. If
multiple pollers ever share a results queue, a receive by one hides messages
from the others for the visibility timeout, so that timeout must be sized
well below any caller's total re-poll budget (it is provisioned alongside
the lane, sized to the inference budget plus long-poll).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from ._env import _lazy_boto3_client, _read_cave_enable_flag
from .results import handle_result
from .schema import FallbackResult

log = logging.getLogger("spark_cave.poll")

# How many messages to pull per ReceiveMessage. A results FIFO interleaves
# results for many callers' requests; one poll scans a batch looking for OUR
# request_id and leaves everyone else's messages untouched. SQS caps a single
# ReceiveMessage at 10.
RECEIVE_BATCH_SIZE = 10

# How many receive batches one `fetch_result_for` call will scan before
# giving up (returning pending). Bounds the per-request work so a deep queue
# can't make one poll run unbounded -- "not found this pass" is just
# pending; the caller is expected to re-poll.
MAX_RECEIVE_BATCHES = 3

# Long-poll wait (seconds) on each ReceiveMessage: SQS holds the call open up
# to this long for a message to arrive instead of returning empty
# immediately, so one waited receive replaces a busy short-poll loop -- far
# fewer ReceiveMessage calls per pending caller, and lower latency when the
# result lands mid-wait. Kept short enough that a caller's own request
# budget (whatever wraps `fetch_result_for`) stays responsive across
# `MAX_RECEIVE_BATCHES` waits; the results queue itself is sized for
# long-poll (a receive_wait_time_seconds around this value).
RECEIVE_WAIT_SECONDS = 2

# A result older than this is ABANDONED: nothing will ever claim it (every
# caller gives up polling well before this). See DECISION 2 above.
ABANDONED_RESULT_MAX_AGE_SECONDS = 600.0

CaveStatus = Literal["pending", "ready", "failed"]


class SQSReceiver(Protocol):
    """The narrow SQS surface the drain loop needs: the boto3 sync client's
    receive/delete. A Protocol (not `object`) so injected fakes are checked
    and the dependency surface is explicit."""

    def receive_message(self, **kwargs) -> dict: ...
    def delete_message(self, **kwargs) -> object: ...


class S3Getter(Protocol):
    """Fetch a spilled result payload's bytes by its `{bucket, key}` pointer
    (sync; hopped onto a thread by this module, never called directly on the
    event loop)."""

    def __call__(self, pointer: dict) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CaveOutcome:
    """The honest outcome of one `fetch_result_for` attempt.

    `pending` -- no matching result on the queue yet (the caller re-polls).
    `ready`   -- a result arrived and was successfully parsed; `parsed` is
                 whatever the injected `parse` callback returned.
    `failed`  -- the connector reported `ok=false`, OR the payload could not
                 be parsed/transformed into something usable; honest
                 failure, NEVER a fabricated `parsed` value (`parsed` stays
                 `None`).

    `detail` carries a short, bounded, whitelisted-charset failure kind when
    the connector said `ok=false` AND supplied one (e.g. a machine-readable
    reject reason) -- present only on that branch, so a caller can render an
    honest, specific empty state instead of a generic "couldn't reach the
    cave" for every `failed`. A `parse` failure (malformed/garbled payload)
    carries no `detail`: the connector did not say why, there is nothing
    honest to report beyond "failed."
    """

    status: CaveStatus
    parsed: object | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CaveResultChannel:
    """Everything one persona's drain loop needs. `sqs` is a boto3-style SQS
    client (sync `receive_message`/`delete_message`); `get_s3` (optional)
    fetches a spilled-result payload by its `{bucket, key}` pointer (also
    sync). Both are hopped onto a thread inside `fetch_result_for` so an
    async caller never blocks on the SDK's blocking I/O."""

    sqs: SQSReceiver
    results_queue_url: str
    persona: str
    get_s3: S3Getter | None = None


_S3_KEY_ALLOWLIST = re.compile(r"[A-Za-z0-9._/-]{1,512}")


def _is_allowlisted_s3_key(key: object) -> bool:
    """True only for a bounded, conservative-charset, traversal-free string
    key. Connectors write program-generated keys; anything outside this
    shape is a manipulated pointer, not a real spill."""
    return (
        isinstance(key, str)
        and bool(_S3_KEY_ALLOWLIST.fullmatch(key))
        and not key.startswith("/")
        and ".." not in key
    )


def build_result_channel_from_env(
    prefix: str,
    *,
    persona: str,
    sqs_factory: Callable[[], object] | None = None,
    s3_factory: Callable[[], object] | None = None,
) -> CaveResultChannel | None:
    """Build a `CaveResultChannel` from env, or None when disabled/unconfigured.

    Enabled only when `<prefix>_CAVE_ENABLED` is truthy AND
    `SPARK_CAVE_<prefix>_RESULTS_QUEUE_URL` is present -- the same template
    `lane.build_lane_from_env` uses for its jobs queue, with `RESULTS` in
    place of `JOBS`. Either missing -> None, preserving the honest-empty/
    honest-disabled behaviour every consumer already relies on. The S3
    getter is wired only when `SPARK_CAVE_<prefix>_PAYLOAD_BUCKET` is also
    set (the spillover path is the rare branch -- most results are tiny).
    Factories are injectable for tests; default to real lazily-built boto3
    clients so importing this module needs no AWS. The package declares no
    runtime dependencies, so if a channel is enabled without an injected
    factory and boto3 is absent, this raises an ImportError naming both
    remedies. A set-but-unrecognized enable value logs a warning and
    disables the channel (see DECISION 1 in the module docstring).
    """
    enabled_var = f"{prefix}_CAVE_ENABLED"
    queue_var = f"SPARK_CAVE_{prefix}_RESULTS_QUEUE_URL"
    bucket_var = f"SPARK_CAVE_{prefix}_PAYLOAD_BUCKET"

    if not _read_cave_enable_flag(prefix, persona, kind_label="result channel"):
        return None
    queue_url = os.getenv(queue_var, "").strip()
    if not queue_url:
        log.warning("%s enabled but %s unset; %s result channel disabled", enabled_var, queue_var, persona)
        return None

    if sqs_factory is None:
        # The package deliberately declares ZERO runtime dependencies; this
        # default factory is the one boto3-touching convenience, resolved
        # only when a channel is actually enabled without an injected
        # factory. Explicit Session().client() (use_session=True) rather
        # than the bare module-level convenience -- see the module
        # docstring's note on why the drain side takes the more defensive
        # form.
        sqs_factory = _lazy_boto3_client(
            "sqs", enabled_var=enabled_var, kind_label="result channel", use_session=True
        )

    get_s3: S3Getter | None = None
    bucket = os.getenv(bucket_var, "").strip()
    if bucket:
        if s3_factory is None:
            s3_factory = _lazy_boto3_client(
                "s3", enabled_var=bucket_var, kind_label="s3 spillover", use_session=True
            )

        s3_client = s3_factory()
        allowed_bucket = bucket

        def get_s3(pointer: dict) -> bytes:
            # BOTH pointer fields are message-controlled and BOTH are
            # allowlisted before any S3 call. Bucket: the env-configured
            # bucket is the ONLY bucket this channel may read -- honoring a
            # message-supplied bucket would let queue content expand the
            # read surface past the lane's IAM intent. Key: bounded,
            # conservative charset, no traversal shapes -- connectors write
            # program-generated keys, so anything outside this shape is a
            # manipulated pointer, not a real spill.
            if pointer.get("bucket") != allowed_bucket:
                raise ValueError(
                    f"cave result s3 pointer names bucket {pointer.get('bucket')!r}, "
                    f"but this channel is configured for {allowed_bucket!r}"
                )
            key = pointer.get("key")
            if not _is_allowlisted_s3_key(key):
                # Deliberately does NOT echo the key: this message becomes
                # log content via the caller's exception handler, and a
                # manipulated pointer's content does not belong in logs.
                klen = len(key) if isinstance(key, str) else -1
                raise ValueError(
                    f"cave result s3 pointer key fails the allowlist (type={type(key).__name__}, len={klen})"
                )
            obj = s3_client.get_object(Bucket=allowed_bucket, Key=key)
            return obj["Body"].read()

    return CaveResultChannel(
        sqs=sqs_factory(),
        results_queue_url=queue_url,
        persona=persona,
        get_s3=get_s3,
    )


def _match(body: str, *, persona: str, request_id: str) -> FallbackResult | None:
    """Parse one results-queue message body and return the `FallbackResult`
    IFF it is for `persona` and `request_id`; else None.

    Goes through the shared `results.handle_result` dispatcher so the
    never-raises + unknown-persona guarantees hold: a malformed body or a
    result for a different persona/request yields None, never an exception
    into the drain loop. The handler captures a match into a one-slot list
    (no loop-variable closure)."""
    captured: list[FallbackResult] = []

    def _on_match(result_obj: FallbackResult) -> None:
        # Only capture OUR request; another caller's result is left alone
        # for its own poller to find.
        if result_obj.request_id == request_id:
            captured.append(result_obj)

    handle_result(body, handlers={persona: _on_match})
    return captured[0] if captured else None


def _unwrap_payload(result: dict, get_s3: S3Getter | None) -> dict:
    """A result payload may carry its domain data inline OR (rare, over an
    SQS-size threshold) as an s3 pointer the connector spilled to. An s3 ref
    is `{"kind": "s3", "s3": {bucket, key}}`; anything else IS the inline
    payload directly (not nested under an "inline" key -- the wire shape a
    result carries here is intentionally simpler than `packing.PayloadRef`,
    matching what every consuming persona's result payload has always been).
    """
    if isinstance(result, dict) and result.get("kind") == "s3":
        ref = result.get("s3")
        if not isinstance(ref, dict):
            raise ValueError("cave result s3 ref missing its pointer")
        if get_s3 is None:
            raise ValueError("cave result spilled to s3 but no get_s3 configured")
        return json.loads(get_s3(ref))
    return result


async def fetch_result_for(
    channel: CaveResultChannel,
    *,
    request_id: str,
    parse: Callable[[dict], Awaitable[object] | object],
) -> CaveOutcome:
    """Drain the results FIFO looking for `request_id`; return the honest
    outcome. Scans up to `MAX_RECEIVE_BATCHES` batches per call so a deep
    queue can't make one poll run unbounded -- "not found this pass" is
    honest `pending` (the caller is expected to re-poll).

    All SQS/S3 SDK calls are hopped onto a thread so an async caller's event
    loop is never blocked. A poison body never raises (the shared
    `results.handle_result` guarantee); it's logged and skipped, left on the
    queue for its rightful poller. A transient SQS failure on the receive
    call itself is mapped HERE to honest `pending` so "queue read hiccup ->
    re-poll, never a crash" actually holds.

    `parse` receives the already-unwrapped result payload (a `dict` -- see
    DECISION 3 in the module docstring) and returns the persona's domain
    object; it may be async to run further async work (e.g. re-scoring
    against the caller's current state). Any exception it raises, OR an
    empty/falsy return, maps to `failed` with no `detail` (see DECISION 4).
    A matched message is deleted once resolved -- on `ready` AND on
    `failed` alike, since redelivery of an already-resolved match is
    idempotent (a redelivered `ready` result re-parses to the same domain
    object; a redelivered `failed` result re-fails the same way) and would
    otherwise sit forever un-claimable.
    """
    for _ in range(MAX_RECEIVE_BATCHES):
        try:
            resp = await asyncio.to_thread(
                channel.sqs.receive_message,
                QueueUrl=channel.results_queue_url,
                MaxNumberOfMessages=RECEIVE_BATCH_SIZE,
                WaitTimeSeconds=RECEIVE_WAIT_SECONDS,
                MessageSystemAttributeNames=["SentTimestamp"],
            )
        except Exception as e:
            # A throttle / NonExistentQueue / network blip on the receive is
            # a transient read failure: honest pending (the caller
            # re-polls), never a crash.
            log.warning(
                "spark_cave.poll.receive_failed",
                extra={"request_id": request_id, "reason": str(e), "exc_type": type(e).__name__},
                exc_info=True,
            )
            return CaveOutcome(status="pending")
        messages = resp.get("Messages", []) if isinstance(resp, dict) else []
        if not messages:
            return CaveOutcome(status="pending")

        for msg in messages:
            result_obj = _match(msg.get("Body", ""), persona=channel.persona, request_id=request_id)
            if result_obj is None:
                # Not our request (or unparseable, or a different persona).
                # An ABANDONED result -- older than any live poll window --
                # will never be claimed, and on a FIFO queue it blocks its
                # whole group's head. Purge it; a fresh result is never
                # this old. See DECISION 2 in the module docstring.
                await _purge_if_abandoned(channel, msg, request_id)
                continue
            return await _resolve_matched(channel, result_obj, msg, parse)

    # Scanned the batch budget without finding our request -> still pending.
    return CaveOutcome(status="pending")


async def _purge_if_abandoned(channel: CaveResultChannel, msg: dict, request_id: str) -> None:
    """Delete a non-matching result message that no poller can ever claim.

    Never raises: purge is best-effort hygiene -- failure just means the
    message lives until the DLQ redrive or the next scan gets it."""
    try:
        sent_ms = float((msg.get("Attributes") or {}).get("SentTimestamp", ""))
    except (TypeError, ValueError):
        return
    age = time.time() - sent_ms / 1000.0
    if age <= ABANDONED_RESULT_MAX_AGE_SECONDS:
        return
    try:
        await asyncio.to_thread(
            channel.sqs.delete_message,
            QueueUrl=channel.results_queue_url,
            ReceiptHandle=msg.get("ReceiptHandle", ""),
        )
        log.info(
            "spark_cave.poll.abandoned_purged",
            extra={"request_id": request_id, "age_seconds": int(age)},
        )
    except Exception as e:
        log.warning(
            "spark_cave.poll.abandoned_purge_failed",
            extra={"request_id": request_id, "reason": str(e), "exc_type": type(e).__name__},
            exc_info=True,
        )


async def _resolve_matched(
    channel: CaveResultChannel,
    result_obj: FallbackResult,
    msg: dict,
    parse: Callable[[dict], Awaitable[object] | object],
) -> CaveOutcome:
    """Turn the matched `FallbackResult` into the final outcome, deleting the
    message once resolved (so it doesn't redeliver forever)."""
    receipt = msg.get("ReceiptHandle")

    async def _delete() -> None:
        # Best-effort consume. A delete failure (boto ClientError / network
        # blip) must NOT discard an already-resolved result or crash the
        # caller: the message just redelivers, and re-resolving is
        # idempotent. So swallow + log rather than letting it bubble.
        if receipt is None:
            return
        try:
            await asyncio.to_thread(
                channel.sqs.delete_message,
                QueueUrl=channel.results_queue_url,
                ReceiptHandle=receipt,
            )
        except Exception as e:
            log.warning(
                "spark_cave.poll.delete_failed",
                extra={"request_id": result_obj.request_id, "reason": str(e), "exc_type": type(e).__name__},
                exc_info=True,
            )

    if not result_obj.ok:
        # The connector reported a failure -> honest failed. Consume it.
        # Bounded + whitelisted-charset FIRST: this string may travel to a
        # caller AND to the log pipeline -- never log the raw connector
        # value (arbitrary payload text does not belong in log storage).
        err = str(result_obj.error or "")[:64]
        detail = err if re.fullmatch(r"[A-Za-z0-9_:]{1,64}", err) else None
        log.warning(
            "spark_cave.poll.failed",
            extra={"request_id": result_obj.request_id, "error": detail or "<unwhitelisted-detail-dropped>"},
        )
        await _delete()
        return CaveOutcome(status="failed", detail=detail)

    try:
        # `_unwrap_payload` may call `channel.get_s3` (a blocking boto3
        # GetObject) for the spillover path; `parse` may itself run further
        # async work. Both are covered by this one try/except -- see
        # DECISION 4 in the module docstring.
        payload = await asyncio.to_thread(_unwrap_payload, result_obj.result or {}, channel.get_s3)
        parsed = parse(payload)
        if inspect.isawaitable(parsed):
            parsed = await parsed
    except Exception as e:
        # ok=true but the payload was garbled (the persona's `parse` raised)
        # OR the S3 spillover fetch failed/was miswired. EITHER way it's an
        # honest failed -- NEVER a fabricated domain object, and never a
        # crash bubbling out of the caller. The message is consumed so a
        # poison result can't redeliver-storm.
        log.warning(
            "spark_cave.poll.garbled",
            extra={"request_id": result_obj.request_id, "reason": str(e), "exc_type": type(e).__name__},
            exc_info=True,
        )
        await _delete()
        return CaveOutcome(status="failed")

    await _delete()
    if not parsed:
        # `parse` ran clean but produced nothing usable (e.g. every
        # candidate it derived was filtered/deduped away) -> nothing honest
        # to hand back. See DECISION 4 in the module docstring.
        return CaveOutcome(status="failed")
    return CaveOutcome(status="ready", parsed=parsed)


__all__ = (
    "ABANDONED_RESULT_MAX_AGE_SECONDS",
    "MAX_RECEIVE_BATCHES",
    "RECEIVE_BATCH_SIZE",
    "RECEIVE_WAIT_SECONDS",
    "CaveOutcome",
    "CaveResultChannel",
    "CaveStatus",
    "S3Getter",
    "SQSReceiver",
    "build_result_channel_from_env",
    "fetch_result_for",
)
