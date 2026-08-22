"""Address normalisation (:mod:`apps.common.addresses`)."""

import pytest

from apps.common.addresses import normalize_email, normalize_phone


class TestNormalizePhone:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+15550101234", "+15550101234"),
            ("+1 (555) 010-1234", "+15550101234"),
            ("+1-555-010-1234", "+15550101234"),
            ("  +44 7700 900123  ", "+447700900123"),
            # The ITU international access prefix is unambiguous once stripped:
            # no E.164 country code starts with a zero.
            ("00447700900123", "+447700900123"),
            ("0044 7700 900123", "+447700900123"),
            # Unicode dashes look like hyphens and are not hyphens.
            ("+1–555–010–1234", "+15550101234"),
            ("+15550101234\x00", "+15550101234"),
        ],
    )
    def test_it_normalises_international_numbers(self, raw: str, expected: str) -> None:
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            # THE case this module exists for: a national number is not an
            # address until someone says which country. Guessing merges strangers.
            "5550101234",
            "(555) 010-1234",
            "555-0123",
            "",
            "   ",
            "+",
            "++4477",
            "+1234abc567",
            "tel:+15550101234",
            # A trunk prefix that survived: "+0..." is a national number wearing
            # an international costume.
            "+0447700900123",
            # Too short and too long for E.164.
            "+12345",
            "+1234567890123456",
        ],
    )
    def test_it_refuses_what_it_cannot_know(self, raw: str) -> None:
        assert normalize_phone(raw) == ""

    def test_it_never_raises_on_a_non_string(self) -> None:
        """Its inputs include a webhook's platform_user_id (baseline §2)."""
        assert normalize_phone(None) == ""  # type: ignore[arg-type]
        assert normalize_phone(12345) == ""  # type: ignore[arg-type]

    def test_it_is_idempotent(self) -> None:
        once = normalize_phone("+1 (555) 010-1234")
        assert normalize_phone(once) == once


class TestNormalizeEmail:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Foo.Bar@Example.COM", "foo.bar@example.com"),
            ("  a@b.co  ", "a@b.co"),
            ("a@x.com\x00", "a@x.com"),
        ],
    )
    def test_it_case_folds_and_trims(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize("raw", ["", "bad", "a@@b.com", "a b@c.com", "@x.com", "a@", "a@x", "a b"])
    def test_it_refuses_anything_that_is_not_an_address(self, raw: str) -> None:
        assert normalize_email(raw) == ""

    def test_it_folds_the_local_part_too(self) -> None:
        """RFC 5321 says the local part is case-sensitive; no mailbox provider
        agrees, and apps.contacts.services stores the column lowercased — so a
        case-preserving comparison would never match the column it is compared
        against."""
        assert normalize_email("ALICE@example.com") == normalize_email("alice@EXAMPLE.com")

    def test_it_never_raises_on_a_non_string(self) -> None:
        assert normalize_email(None) == ""  # type: ignore[arg-type]
