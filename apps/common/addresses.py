"""Normalising the two addresses that identify a person across channels.

Issue #8 links an inbound SMS or email identity to an existing contact *by
address*, and "the same address" has to mean the same thing on both sides of
that comparison. ``apps.contacts.services.create_contact`` already lowercases
email on the way in; phone numbers arrive from a platform in whatever shape the
platform sends and from an operator in whatever shape they typed.

**Nothing here guesses a country.** ``normalize_phone("5550101234")`` returns
``""`` rather than assuming ``+1``: a national-format number is not an address
until someone says which country it belongs to, and this project stores no
default region for a workspace or a connection. An unnormalisable number simply
does not match, so the caller creates a second contact — the conservative
failure, and a recoverable one. Guessing wrong merges two strangers'
conversations into one thread, which is not recoverable from a support ticket.

That is also why there is no ``phonenumbers`` dependency. Full libphonenumber
parsing needs exactly the default region this module declines to invent, so it
would buy accuracy only on the input shapes we deliberately refuse to interpret.

Both functions are total: they answer ``""`` for anything they cannot normalise,
and never raise. Their inputs are attacker-controlled (SECURITY-BASELINE §2) —
a webhook's ``platform_user_id`` is one of the call sites.
"""

import re

__all__ = ["E164_MAX_DIGITS", "E164_MIN_DIGITS", "normalize_email", "normalize_phone"]

#: E.164 caps a number at 15 digits including the country code. The floor is
#: lower than most numbers you will meet: +683 xxxx (Niue) is seven digits in
#: total, and rejecting it would be a silent data-quality bug in one deployment
#: rather than a safety property in every other.
E164_MIN_DIGITS = 7
E164_MAX_DIGITS = 15

#: Separators a human or a platform may put inside a number. The dash class
#: covers the Unicode dashes that word processors and phone keyboards emit,
#: which look identical to a hyphen and are not one.
_PHONE_SEPARATORS = re.compile(r"[\s()\[\]./‐-―−-]+")

_ALL_DIGITS = re.compile(r"\A[0-9]+\Z")

#: NUL cannot be stored in a Postgres text column at all, and the rest of C0 has
#: no business in an address. Stripped rather than rejected: a trailing \r from
#: a CSV is not a hostile payload, it is a Tuesday.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_email(value: str) -> str:
    """A lowercased, whitespace-free email address, or ``""``.

    Case-folds the whole address, including the local part. RFC 5321 says the
    local part is case-*sensitive*, and in practice no mailbox provider treats
    it that way — while ``apps.contacts.services.create_contact`` already stores
    ``email`` lowercased, so a comparison that preserved case would simply never
    match the column it is compared against.
    """
    if not isinstance(value, str):
        return ""
    cleaned = _CONTROL.sub("", value).strip().lower()
    # One "@", something either side, and no whitespace left inside. Deliverability
    # is the email adapter's problem (#21); this only has to decide whether two
    # strings name the same mailbox.
    if cleaned.count("@") != 1 or any(ch.isspace() for ch in cleaned):
        return ""
    local, _, domain = cleaned.partition("@")
    if not local or not domain or "." not in domain:
        return ""
    return cleaned


def normalize_phone(value: str) -> str:
    """``value`` as an E.164 number (``+`` then digits), or ``""``.

    Accepts a leading ``+`` or the ``00`` international prefix and strips the
    separators people type. Returns ``""`` for anything else — most importantly
    for a bare national number, which this module will not guess a country code
    for. See the module docstring.

    >>> normalize_phone("+1 (555) 010-1234")
    '+15550101234'
    >>> normalize_phone("00447700900123")
    '+447700900123'
    >>> normalize_phone("5550101234")
    ''
    """
    if not isinstance(value, str):
        return ""
    cleaned = _PHONE_SEPARATORS.sub("", _CONTROL.sub("", value)).strip()
    if not cleaned:
        return ""

    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif cleaned.startswith("00"):
        # The ITU international access prefix. Unambiguous once the leading
        # zeros are gone, because E.164 country codes never start with 0.
        digits = cleaned[2:]
    else:
        return ""

    if not _ALL_DIGITS.match(digits):
        return ""
    if not E164_MIN_DIGITS <= len(digits) <= E164_MAX_DIGITS:
        return ""
    if digits.startswith("0"):
        # No E.164 country code begins with 0, so this is a trunk prefix that
        # survived — a national number wearing a "+", not an international one.
        return ""
    return f"+{digits}"
