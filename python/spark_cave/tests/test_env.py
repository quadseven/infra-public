"""Tests for the shared env-gating primitives (spark_cave._env).

Pins the two contracts lane.py and poll.py both depend on: the enable-flag
truth table (identical to what test_lane.py/test_poll.py already pin at the
builder level -- this is the unit-level parity proof), and that
`_lazy_boto3_client` raises the same actionable ImportError shape whether
boto3 is genuinely absent (module-level fake) and honors `use_session`.
"""

from __future__ import annotations

import builtins
import logging
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[1]
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import pytest  # noqa: E402

from spark_cave._env import _lazy_boto3_client, _read_cave_enable_flag  # noqa: E402


class _FakeBoto3:
    """Fake `boto3` module: records which of the two DISTINCT construction
    paths was taken (bare `boto3.client()` vs `boto3.session.Session()
    .client()`), without requiring the real (deliberately-not-a-dependency)
    package. Matches this package's stated "fully unit-testable with
    fakes" design. The two paths must record differently-tagged calls, or a
    test asserting `use_session` took effect could pass for the wrong
    reason."""

    def __init__(self):
        self.calls = []
        self.session = _FakeSession(self.calls)

    def client(self, service):
        self.calls.append(("client", service))
        return ("client", service)


class _FakeSession:
    def __init__(self, calls):
        self._calls = calls

    def Session(self):  # noqa: N802 - matches boto3.session.Session's real name
        return self

    def client(self, service):
        self._calls.append(("session.client", service))
        return ("session.client", service)


@pytest.fixture
def fake_boto3(monkeypatch):
    fake = _FakeBoto3()
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return fake


class TestReadCaveEnableFlag:
    def test_unset_is_false_and_quiet(self, monkeypatch, caplog):
        monkeypatch.delenv("TEST_CAVE_ENABLED", raising=False)
        with caplog.at_level(logging.WARNING):
            assert _read_cave_enable_flag("TEST", "persona", kind_label="lane") is False
        assert caplog.records == []

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("TEST_CAVE_ENABLED", value)
        assert _read_cave_enable_flag("TEST", "persona", kind_label="lane") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_explicit_falsy_disables_silently(self, monkeypatch, caplog, value):
        monkeypatch.setenv("TEST_CAVE_ENABLED", value)
        with caplog.at_level(logging.WARNING):
            assert _read_cave_enable_flag("TEST", "persona", kind_label="lane") is False
        assert caplog.records == []

    def test_garbage_value_warns_once_and_disables(self, monkeypatch, caplog):
        monkeypatch.setenv("TEST_CAVE_ENABLED", "treu")
        with caplog.at_level(logging.WARNING):
            result = _read_cave_enable_flag("TEST", "some-persona", kind_label="lane")
        assert result is False
        assert len(caplog.records) == 1
        msg = caplog.records[0].message
        assert "TEST_CAVE_ENABLED" in msg
        assert "treu" in msg
        assert "some-persona" in msg
        assert "lane" in msg

    def test_kind_label_is_used_in_logger_name_and_message(self, monkeypatch, caplog):
        monkeypatch.setenv("TEST_CAVE_ENABLED", "garbage")
        with caplog.at_level(logging.WARNING):
            _read_cave_enable_flag("TEST", "persona", kind_label="result channel")
        assert caplog.records[0].name == "spark_cave.result channel"
        assert "result channel" in caplog.records[0].message


class TestLazyBoto3Client:
    def test_missing_boto3_raises_actionable_error(self, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("No module named 'boto3'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match="install boto3 or"):
            _lazy_boto3_client("sqs", enabled_var="TEST_CAVE_ENABLED", kind_label="lane")

    def test_returns_a_client_factory_not_a_client(self, fake_boto3):
        factory = _lazy_boto3_client("sqs", enabled_var="TEST_CAVE_ENABLED", kind_label="lane")
        assert callable(factory)
        assert fake_boto3.calls == []  # not invoked until the factory is called

    def test_use_session_false_calls_module_level_client(self, fake_boto3):
        factory = _lazy_boto3_client("sqs", enabled_var="X", kind_label="lane", use_session=False)
        result = factory()
        assert result == ("client", "sqs")
        assert fake_boto3.calls == [("client", "sqs")]

    def test_use_session_true_calls_session_client(self, fake_boto3):
        factory = _lazy_boto3_client("s3", enabled_var="X", kind_label="s3 spillover", use_session=True)
        result = factory()
        assert result == ("session.client", "s3")
        assert fake_boto3.calls == [("session.client", "s3")]
