"""Env-gated Spark-Cave enqueue lane, shared across personas.

Hoists the enqueue-side lifecycle that each consuming persona otherwise
defines independently: an env-gated `CaveLane` (queue URL + injected sync SQS
client + persona) built once at startup, and a thread-hopped `enqueue` that
calls the existing `spark_cave.enqueue.enqueue` off the event loop.

Additive: importing this module changes no call site and no behaviour of the
other submodules.

DECISION -- env-var naming template (read before touching `build_lane_from_env`):
a lane's two env vars do NOT share one literal prefix. Both live consumers fit
exactly ONE template keyed on a single token, passed here as `prefix`:

    <PREFIX>_CAVE_ENABLED
    SPARK_CAVE_<PREFIX>_JOBS_QUEUE_URL

(e.g. prefix "MACCHINA" -> MACCHINA_CAVE_ENABLED +
SPARK_CAVE_MACCHINA_JOBS_QUEUE_URL; prefix "MACCHINA_COACH" ->
MACCHINA_COACH_CAVE_ENABLED + SPARK_CAVE_MACCHINA_COACH_JOBS_QUEUE_URL).
`build_lane_from_env` derives both names from the single `prefix`, preserving
the live consumers' names byte-for-byte. A naive shared-prefix reading
("<PREFIX>_ENABLED" / "<PREFIX>_JOBS_QUEUE_URL") would produce
`MACCHINA_CAVE_JOBS_QUEUE_URL`, which matches no live consumer -- this module
encodes the naming actually read from the consumers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from .enqueue import DEFAULT_INLINE_THRESHOLD_BYTES
from .enqueue import enqueue as _sc_enqueue
from .schema import FallbackJob

log = logging.getLogger("spark_cave.lane")

# Same truthy set both `cave_tail.py` and `coach_cave_tail.py` use today.
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


@dataclass(frozen=True, slots=True)
class CaveLane:
    """Everything one persona's tail needs to enqueue. `sqs` is a boto3-style
    SQS client (sync `send_message`); `enqueue` hops it onto a thread so an
    async request handler never blocks on the SDK's blocking I/O."""

    sqs: object
    jobs_queue_url: str
    persona: str


def build_lane_from_env(
    prefix: str,
    *,
    persona: str,
    sqs_factory: Callable[[], object] | None = None,
) -> CaveLane | None:
    """Build a `CaveLane` from env, or None when disabled/unconfigured.

    Enabled only when `<prefix>_CAVE_ENABLED` is truthy AND
    `SPARK_CAVE_<prefix>_JOBS_QUEUE_URL` is present -- see the module
    docstring for why the two vars don't share one literal prefix. Either
    missing -> None, preserving today's honest-empty/honest-disabled
    behaviour: `cave_tail.build_cave_from_env` / `coach_cave_tail.build_cave_from_env`
    both return None the same way, for the same reason. `sqs_factory` is
    injectable for tests; defaults to a real boto3 SQS client built lazily so
    importing this module needs no AWS. The package declares no runtime
    dependencies, so if a lane is enabled without an injected factory and
    boto3 is absent, this raises an ImportError naming both remedies. A
    set-but-unrecognized enable value logs a warning and disables the lane.
    """
    enabled_var = f"{prefix}_CAVE_ENABLED"
    queue_var = f"SPARK_CAVE_{prefix}_JOBS_QUEUE_URL"

    raw = os.getenv(enabled_var, "").strip().lower()
    if raw not in _TRUTHY:
        # Unset or an explicit "off" is a normal disabled lane -- stay quiet.
        # Anything else set is a misconfiguration (a typo'd "treu" would
        # otherwise silently disable the lane); warn so it is diagnosable.
        if raw and raw not in _FALSY:
            log.warning(
                "%s=%r is not a recognized boolean; %s lane disabled (use one of %s to enable)",
                enabled_var,
                raw,
                persona,
                sorted(_TRUTHY),
            )
        return None
    queue_url = os.getenv(queue_var, "").strip()
    if not queue_url:
        log.warning("%s enabled but %s unset; %s lane disabled", enabled_var, queue_var, persona)
        return None

    if sqs_factory is None:
        # The package deliberately declares ZERO runtime dependencies
        # (schema/packing/enqueue are all injection-based); this default
        # factory is the one boto3-touching convenience, resolved only when
        # a lane is actually enabled without an injected factory.
        try:
            import boto3  # local import: keep module import AWS-free
        except ImportError as exc:
            raise ImportError(
                "spark_cave declares no runtime dependencies; to enable a "
                f"cave lane ({enabled_var} is set) either install boto3 or "
                "pass sqs_factory= explicitly"
            ) from exc

        def _default_sqs_factory():
            return boto3.client("sqs")

        sqs_factory = _default_sqs_factory

    return CaveLane(sqs=sqs_factory(), jobs_queue_url=queue_url, persona=persona)


async def enqueue(
    lane: CaveLane,
    *,
    principal_id: str,
    request_id: str,
    payload: dict,
    put_s3: Callable[[bytes], dict] | None = None,
    inline_threshold_bytes: int = DEFAULT_INLINE_THRESHOLD_BYTES,
) -> FallbackJob:
    """Thread-hop `spark_cave.enqueue.enqueue` off the event loop, using
    `lane`'s persona + queue URL + injected SQS client for the FIFO
    `MessageGroupId` (`persona:principal_id`) and `MessageDeduplicationId`
    (`persona:principal_id:request_id`). Returns the `FallbackJob` that was
    sent, unchanged from the synchronous call.

    This is exactly what `cave_tail.enqueue_meal_job` and
    `coach_cave_tail.enqueue_coach_job` already do today (generate a
    request_id, build a persona payload, call `spark_cave.enqueue.enqueue` via
    `asyncio.to_thread`) -- generalized over persona/env-prefix instead of
    each hardcoding one. Request_id generation and payload encoding stay the
    caller's job (the persona-specific codec), unchanged in this slice.
    """
    return await asyncio.to_thread(
        _sc_enqueue,
        sqs=lane.sqs,
        queue_url=lane.jobs_queue_url,
        persona=lane.persona,
        principal_id=principal_id,
        request_id=request_id,
        payload=payload,
        put_s3=put_s3,
        inline_threshold_bytes=inline_threshold_bytes,
    )


__all__ = ("CaveLane", "build_lane_from_env", "enqueue")
