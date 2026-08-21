"""Global log scrubbing (SECURITY-BASELINE §5)."""

import importlib
import logging
import time

import pytest

from apps.common.logging import REDACTED, SecretScrubbingFilter, scrub
from tests.testapp.models import EncryptionProbe

SETTINGS_MODULES = [
    "config.settings.base",
    "config.settings.development",
    "config.settings.production",
    "config.settings.test",
]


class TestScrub:
    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("access_token=ya29.A0ARrdaM-verysecretvalue", "ya29.A0ARrdaM-verysecretvalue"),
            ('api_key: "sk-proj-abcdefghijklmnop"', "sk-proj-abcdefghijklmnop"),
            ("{'client_secret': 'GOCSPX-abcdefghijklmnop'}", "GOCSPX-abcdefghijklmnop"),
            ("password=hunter2", "hunter2"),
            ("refresh_token = 1//0gabcdefghijklmnop", "1//0gabcdefghijklmnop"),
            ("X-Verify-Token: abc123def456", "abc123def456"),
            ("webhook signature=sha256=abcdef0123456789", "abcdef0123456789"),
            ('{"credentials": "abcdefghij"}', "abcdefghij"),
        ],
    )
    def test_keyed_secrets_are_redacted(self, text, secret):
        result = scrub(text)

        assert secret not in result
        assert REDACTED in result

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            (
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef123456",
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef123456",
            ),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("using sk_live_51Abcdefghijklmnop to charge", "sk_live_51Abcdefghijklmnop"),
            ("saw ghp_abcdefghijklmnopqrstuvwxyz0123456789 in the diff", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
            ("slack xoxb-1234567890-abcdefghijkl", "xoxb-1234567890-abcdefghijkl"),
            ("aws key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
            (
                "telegram 8123456789:AAH-thisIsNotARealBotToken-0123456789",
                "8123456789:AAH-thisIsNotARealBotToken-0123456789",
            ),
        ],
    )
    def test_credential_shapes_are_redacted_without_a_key_name(self, text, secret):
        """Bare credentials with no ``key=`` context must still be caught."""
        result = scrub(text)

        assert secret not in result
        assert REDACTED in result

    def test_pem_private_keys_are_redacted(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----"

        assert scrub(f"loaded {pem} ok") == f"loaded {REDACTED} ok"

    @pytest.mark.parametrize(
        "text",
        [
            "Contact 4a1f created in workspace demo",
            "Flow published with 12 nodes",
            "GET /healthz 200",
        ],
    )
    def test_ordinary_messages_are_untouched(self, text):
        assert scrub(text) == text

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            # A JSON-encoded value containing a quote: the value pattern used
            # to stop at the escaped quote and emit everything after it.
            (r'{"password": "abc\"remaining-secret"}', "remaining-secret"),
            (r"{'api_key': 'abc\'tail-secret'}", "tail-secret"),
            (r'{"token": "a\\"}', "a\\"),
        ],
    )
    def test_escaped_quotes_do_not_end_the_value_early(self, text, secret):
        assert secret not in scrub(text)

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            # "Basic YTpi" is base64 for "a:b" — short, and still a credential.
            ("Authorization: Basic YTpi", "YTpi"),
            ("Authorization: Bearer abc", "abc"),
            ("Authorization: Token x1", "x1"),
        ],
    )
    def test_short_authorization_credentials_are_redacted(self, text, secret):
        """An explicit auth scheme is unambiguous context; length is irrelevant."""
        result = scrub(text)

        assert secret not in result
        assert REDACTED in result

    def test_is_idempotent(self):
        once = scrub("access_token=abcdef123456")

        assert scrub(once) == once

    def test_stays_linear_on_hostile_input(self):
        """Log lines carry attacker-controlled content (baseline §2).

        A pattern that backtracks badly would turn every inbound message into
        a denial-of-service lever, so pathological input has to stay fast.
        """
        hostile = "token=" + ("a" * 20_000) + " " + ("'" * 5_000) + " Bearer " + ("x" * 20_000)

        start = time.perf_counter()
        scrub(hostile)
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0, f"scrub() took {elapsed:.2f}s on 45k characters"


class TestFilter:
    def test_rewrites_the_record_without_dropping_it(self):
        record = logging.LogRecord(
            name="apps.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="connected with access_token=%s",
            args=("super-secret-value",),
            exc_info=None,
        )

        assert SecretScrubbingFilter().filter(record) is True
        assert "super-secret-value" not in record.getMessage()
        assert REDACTED in record.getMessage()

    def test_survives_broken_format_strings(self):
        record = logging.LogRecord(
            name="apps.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="token=%s and %s",
            args=("only-one",),
            exc_info=None,
        )

        assert SecretScrubbingFilter().filter(record) is True
        assert REDACTED in str(record.msg)


class TestInstallation:
    @pytest.mark.parametrize("module_path", SETTINGS_MODULES)
    def test_every_settings_module_attaches_the_filter_to_every_handler(self, module_path):
        """SECURITY-BASELINE §5: installed in *all* environments, not just prod."""
        module = importlib.import_module(module_path)
        logging_config = module.LOGGING

        assert "scrub_secrets" in logging_config["filters"]
        assert logging_config["filters"]["scrub_secrets"]["()"] == "apps.common.logging.SecretScrubbingFilter"
        for name, handler in logging_config["handlers"].items():
            assert "scrub_secrets" in handler.get("filters", []), f"handler {name!r} is not scrubbed"

    def test_record_factory_is_installed(self):
        """The AppConfig.ready() hook covers handlers LOGGING does not own."""
        record = logging.getLogRecordFactory()(
            "apps.test", logging.INFO, __file__, 1, "api_key=%s", ("leaked-value",), None
        )

        assert "leaked-value" not in record.getMessage()


@pytest.mark.django_db
class TestEncryptedFieldPlaintextNeverReachesLogs:
    """The acceptance test from issue #2: an encrypted field's plaintext must
    not survive a trip through the logging pipeline, including pytest's own
    capture handler (which the LOGGING filters never see).

    ``secret_value`` is deliberately shapeless, so each case here can only pass
    if the rule it names actually fires — no single pattern covers the set.
    """

    @pytest.mark.parametrize(
        ("template", "rule"),
        [
            ("connected with access_token=%s", "key=value"),
            ("Authorization: Bearer %s", "auth scheme"),
            ("credentials: '%s'", "quoted key: value"),
            ("payload=%r", "dict repr"),
        ],
    )
    def test_plaintext_is_absent_from_captured_logs(self, caplog, secret_value, template, rule):
        probe = EncryptionProbe.objects.create(secret=secret_value)
        stored = EncryptionProbe.objects.get(pk=probe.pk).secret
        assert stored == secret_value  # the value really is the live credential

        logger = logging.getLogger("apps.common.tests")
        with caplog.at_level(logging.INFO):
            logger.info(template, {"client_secret": stored} if "%r" in template else stored)

        assert secret_value not in caplog.text, f"the {rule} rule did not redact it"
        assert REDACTED in caplog.text

    def test_plaintext_is_absent_from_captured_tracebacks(self, caplog, secret_value):
        """The commonest leak path: a credential inside an exception message."""
        probe = EncryptionProbe.objects.create(secret=secret_value)
        stored = EncryptionProbe.objects.get(pk=probe.pk).secret

        logger = logging.getLogger("apps.common.tests")
        with caplog.at_level(logging.ERROR):
            try:
                raise ValueError(f"upstream rejected api_key={stored}")
            except ValueError:
                logger.exception("send failed")

        assert secret_value not in caplog.text
        # The traceback still has to be there — scrubbing must not eat it.
        assert "ValueError" in caplog.text
        assert REDACTED in caplog.text

    def test_plaintext_is_absent_when_the_format_string_is_broken(self, caplog, secret_value):
        """A logging bug must not become a leak.

        Handler.handleError writes the record's raw args to stderr when
        formatting fails, so the args have to be scrubbed even on this path.
        """
        record = logging.LogRecord(
            name="apps.common.tests",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="api_key=%s and %s",
            args=(secret_value,),
            exc_info=None,
        )
        SecretScrubbingFilter().filter(record)

        assert all(secret_value not in str(arg) for arg in record.args)
        assert secret_value not in str(record.msg)
