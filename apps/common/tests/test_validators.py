"""Shared validators."""

import pytest
from django.core.exceptions import ValidationError

from apps.common.validators import is_valid_hex_color, validate_hex_color

VALID = ["#3B82F6", "#000000", "#ffffff", "#AbCdEf"]
INVALID = ["3B82F6", "#3B82F", "#3B82F6A", "#GGGGGG", "red", "#3b82f6 ", 0x3B82F6]


@pytest.mark.parametrize("value", VALID)
def test_valid_colors_pass(value):
    validate_hex_color(value)
    assert is_valid_hex_color(value)


@pytest.mark.parametrize("value", INVALID)
def test_invalid_colors_fail(value):
    with pytest.raises(ValidationError):
        validate_hex_color(value)
    assert not is_valid_hex_color(value)


@pytest.mark.parametrize("value", ["", None])
def test_empty_means_no_override(value):
    """Empty and None pass so that "no colour override" keeps working."""
    validate_hex_color(value)
    assert is_valid_hex_color(value)
