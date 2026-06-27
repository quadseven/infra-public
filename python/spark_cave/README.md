# spark_cave

Persona-generic **SQS-airlock** library for the Spark-Cave: the owned, async
"reach-out" channel a cloud service uses to hand work to an on-prem connector
(which runs it on self-hosted GPUs) and get a result back. The cloud and the
on-prem side never connect directly — the SQS queues are the only contact
surface.

Canonical PUBLIC home so a public consumer repo and a private consumer repo can
share ONE wire contract without either leaking into the other. Consumers
**vendor this package at build, SHA-pinned** (no runtime PyPI dependency; same
discipline as the reusable workflows here).

## Modules

- `schema` — `FallbackJob` / `FallbackResult` / `PayloadRef`, persona-parameterized,
  JSON round-trip, deterministic FIFO `dedup_key` (`persona:principal:request`).
- `packing` — inline-vs-S3 payload spillover (`pack`/`unpack`); the S3 put/get are
  injected, so it is pure.
- `enqueue` — send a `FallbackJob` to a jobs FIFO with the right `MessageGroupId`
  (`persona:principal`) + `MessageDeduplicationId`; the SQS client is injected.
- `results` — parse a `FallbackResult` and dispatch to a per-persona handler;
  idempotent, never raises back into the consumer (no poison-message retry storm).

Pure stdlib, zero runtime deps. Queue/bucket names are caller-injected — this
library hardcodes none.

## Test

    cd python/spark_cave && uv run pytest

## Provenance

Some modules carry a `Spark-authored:` comment block recording an on-prem model
that generated them (model + host + date + review). `git grep "Spark-authored"`
lists them.
