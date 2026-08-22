"""``DEFAULT_SEND_RATE_OVERRIDES`` is checked at startup, not discovered later."""

from typing import Any

import pytest
from django.test import override_settings

from apps.messaging.checks import check_send_rate_overrides


def codes(**settings: Any) -> list[str | None]:
    with override_settings(**settings):
        return [error.id for error in check_send_rate_overrides()]


class TestTheCheck:
    def test_the_default_is_clean(self) -> None:
        assert codes(DEFAULT_SEND_RATE_OVERRIDES={}) == []

    def test_a_valid_override_is_clean(self) -> None:
        assert codes(DEFAULT_SEND_RATE_OVERRIDES={"telegram": 10, "sms": 0.5}) == []

    def test_a_typo_in_a_platform_name_is_caught(self) -> None:
        """It would otherwise apply to nothing, silently, forever."""
        assert codes(DEFAULT_SEND_RATE_OVERRIDES={"telegrram": 10}) == ["messaging.E002"]

    @pytest.mark.parametrize("rate", [0, -1, "fast", None, True])
    def test_a_rate_that_is_not_a_positive_number_is_caught(self, rate: Any) -> None:
        """Zero is a bucket that never fills, which reads as a silent outage."""
        assert codes(DEFAULT_SEND_RATE_OVERRIDES={"telegram": rate}) == ["messaging.E003"]

    def test_a_non_object_value_is_caught(self) -> None:
        assert codes(DEFAULT_SEND_RATE_OVERRIDES=["telegram", 10]) == ["messaging.E001"]

    def test_it_is_registered_with_django(self) -> None:
        from django.core.checks import registry

        assert check_send_rate_overrides in registry.registry.get_checks()
