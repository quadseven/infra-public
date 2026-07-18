"""Internal env-gating plumbing shared by `lane.py` and `poll.py`.

Underscore-prefixed and NOT re-exported from `__init__` -- this is internal
implementation detail, not part of the package's wire contract. Extracted
because both builders carried byte-for-byte copies of the enable-flag parse
block and the lazy-boto3-client default factory (infra-public#48); a fix to
one copy silently missing the other was the drift risk this module removes.

No behaviour, env-var name, or public signature changes -- see the parity
tests in test_env.py.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _read_cave_enable_flag(prefix: str, persona: str, *, kind_label: str) -> bool:
    """True only when `<prefix>_CAVE_ENABLED` is truthy. Quiet when unset or
    an explicit falsy value (a normal disabled state). Warns once when set
    to something unrecognized (a typo'd "treu" would otherwise silently
    disable the lane/channel with no signal)."""
    enabled_var = f"{prefix}_CAVE_ENABLED"
    raw = os.getenv(enabled_var, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw and raw not in _FALSY:
        logging.getLogger(f"spark_cave.{kind_label}").warning(
            "%s=%r is not a recognized boolean; %s %s disabled (use one of %s to enable)",
            enabled_var,
            raw,
            persona,
            kind_label,
            sorted(_TRUTHY),
        )
    return False


def _lazy_boto3_client(
    service: str,
    *,
    enabled_var: str,
    kind_label: str,
    use_session: bool = False,
) -> Callable[[], object]:
    """Return a zero-arg factory that lazily builds a boto3 `service` client.

    The package declares zero runtime dependencies, so this local-imports
    boto3 only when actually called (i.e. only when a lane/channel is
    enabled without an injected factory) and raises an actionable
    ImportError naming both remedies (install boto3 / pass a factory) if
    it's absent. `use_session=True` builds via `Session().client(...)`
    instead of the bare module-level `boto3.client(...)` convenience -- the
    drain side (poll.py) takes this more defensive form deliberately; keep
    that distinction, don't unify it.
    """
    try:
        import boto3  # local import: keep module import AWS-free
    except ImportError as exc:
        raise ImportError(
            "spark_cave declares no runtime dependencies; to enable a "
            f"cave {kind_label} ({enabled_var} is set) either install "
            f"boto3 or pass a factory explicitly"
        ) from exc

    if use_session:
        return lambda: boto3.session.Session().client(service)
    return lambda: boto3.client(service)
