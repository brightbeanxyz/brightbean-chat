"""Sentry event scrubbing (SECURITY-BASELINE §5).

Sentry is the "error reports" half of the baseline's "never in captured logs,
error reports, admin list displays, or API responses". It does not go through
the logging pipeline at all — its Django integration builds an event from the
exception object — so the log scrubber's guarantees do not extend to it and
these are separate tests, not a rehash.
"""

from unittest.mock import patch

import pytest

from apps.common.logging import REDACTED
from apps.common.sentry import configure_sentry, scrub_event

SECRET = "sk_live_abcdef1234567890"


class TestScrubEvent:
    def test_redacts_the_exception_message(self):
        event = {"exception": {"values": [{"type": "ValueError", "value": f"upstream rejected api_key={SECRET}"}]}}

        result = scrub_event(event)

        assert SECRET not in str(result)
        assert REDACTED in result["exception"]["values"][0]["value"]

    def test_redacts_nested_structures(self):
        event = {
            "breadcrumbs": {"values": [{"message": f"Authorization: Bearer {SECRET}"}]},
            "extra": {"connection": {"credentials": [f"token={SECRET}"]}},
            "request": {"query_string": f"access_token={SECRET}"},
        }

        assert SECRET not in str(scrub_event(event))

    def test_leaves_ordinary_content_alone(self):
        event = {"message": "Flow published with 12 nodes", "level": "info", "extra": {"count": 12}}

        assert scrub_event(event) == event

    def test_preserves_non_string_types(self):
        event = {"timestamp": 1700000000.5, "count": 3, "ok": True, "nothing": None}

        assert scrub_event(event) == event

    def test_never_drops_an_event_when_scrubbing_fails(self):
        """A scrubbing bug must not also cost the error report."""
        event = {"message": "boom"}

        with patch("apps.common.sentry._scrub_value", side_effect=RuntimeError("boom")):
            assert scrub_event(event) is event

    def test_survives_a_cyclic_event(self):
        event: dict = {"extra": {}}
        event["extra"]["self"] = event  # Sentry should not build this, but must not hang us

        assert scrub_event(event) is not None


class TestConfigureSentry:
    """The options matter as much as the hook.

    String scrubbing cannot reach stack locals, and stack locals are exactly
    where a decrypted EncryptedTextField value sits.
    """

    @pytest.fixture
    def init_kwargs(self):
        with patch("sentry_sdk.init") as init:
            configure_sentry("https://public@example.ingest.sentry.io/1")
        return init.call_args.kwargs

    def test_local_variables_are_not_captured(self, init_kwargs):
        assert init_kwargs["include_local_variables"] is False

    def test_pii_and_request_bodies_are_off(self, init_kwargs):
        assert init_kwargs["send_default_pii"] is False
        assert init_kwargs["max_request_body_size"] == "never"

    def test_the_scrubbing_hook_is_installed(self, init_kwargs):
        assert init_kwargs["before_send"] is scrub_event
        assert init_kwargs["event_scrubber"] is not None

    def test_overrides_win(self):
        with patch("sentry_sdk.init") as init:
            configure_sentry("https://public@example.ingest.sentry.io/1", traces_sample_rate=0.5)

        assert init.call_args.kwargs["traces_sample_rate"] == 0.5


@pytest.fixture
def disabled_sentry_client():
    """Tear the real client down again after the test.

    configure_sentry() installs a process-wide client. Left running it holds a
    DSN, delays interpreter exit flushing pending events, and tries to reach
    the network from the test suite.
    """
    import sentry_sdk

    yield
    sentry_sdk.get_client().close(timeout=0.0)
    sentry_sdk.init(dsn=None)


class TestEndToEnd:
    def test_a_real_captured_exception_carries_no_secret(self, disabled_sentry_client):
        """Drives sentry_sdk itself rather than a hand-built event dict."""
        import sentry_sdk

        captured = []

        def before_send(event, hint):
            captured.append(scrub_event(event, hint))
            return None  # never actually transmit

        configure_sentry("https://public@example.ingest.sentry.io/1", before_send=before_send)
        try:
            access_token = SECRET  # noqa: F841 - the point is that it is a local
            raise ValueError(f"upstream rejected api_key={SECRET}")
        except ValueError:
            sentry_sdk.capture_exception()

        assert captured, "sentry_sdk did not build an event"
        assert SECRET not in str(captured[0])
