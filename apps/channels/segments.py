"""SMS segment arithmetic — GSM-7 vs UCS-2, 160/70, concatenation (SPEC §6.6).

A text message is not billed by the character. It is billed by the *segment*,
and how many segments a given string costs depends on an encoding decision the
sender never makes explicitly: if every character fits GSM-7, the message is
packed seven bits to the character and a single segment holds 160 of them; one
character outside that alphabet switches the whole message to UCS-2 and the
segment holds 70. A message that does not fit one segment is split, and each
part loses room to the concatenation header — 153 and 67 respectively — so
going one character over 160 costs **two** segments of 153, not 160 plus 1.

That arithmetic is why this module exists rather than a ``len(text)`` at the
call site. A composer that shows "161 characters" next to a message that costs
double is worse than showing nothing.

**Pure by construction**, the same way :mod:`apps.channels.downgrade` is: no
Django import, no database, no clock, no settings. Same input, same output,
forever. That is what lets the tests be a table, what lets the flow builder's
panel hint and L6-B's broadcast composer share one answer, and what keeps the
per-segment *price* — which is deployment data, not arithmetic — out of here.
The caller multiplies :attr:`SegmentCount.segments` by whatever it has.

Two packing rules that a naive division gets wrong, and both are reachable from
ordinary text:

*An escaped character is indivisible.* ``€`` and the other nine members of the
GSM-7 extension table cost two septets, an ESC followed by the character, and
the pair may not straddle a segment boundary. A message whose 153rd septet
would be that ESC pushes the whole pair into the next segment.

*So is a surrogate pair.* In UCS-2 an emoji outside the basic plane is two
code units, and splitting it produces two segments neither of which contains a
character. Both cases are handled by packing per character rather than by
dividing the total.
"""

from dataclasses import dataclass

__all__ = [
    "GSM7_CONCATENATED",
    "GSM7_SINGLE",
    "UCS2_CONCATENATED",
    "UCS2_SINGLE",
    "Encoding",
    "SegmentCount",
    "segments_for",
]

#: The GSM 03.38 basic alphabet, in code-point order. Every character here costs
#: one septet. Written as a literal rather than derived, because the table *is*
#: the specification and a derivation would be a second, checkable-only-by-eye
#: copy of it.
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

#: The GSM 03.38 extension table. Each of these is transmitted as ESC plus the
#: character, so it costs **two** septets. ``\f`` (form feed) is a member and is
#: included: it is not something a person types, but it is something a payload
#: can contain, and counting it as one septet would under-report the cost.
_GSM7_EXTENDED = "\f^{}\\[~]|€"

_GSM7_BASIC_SET = frozenset(_GSM7_BASIC)
_GSM7_EXTENDED_SET = frozenset(_GSM7_EXTENDED)

#: Septets in a single GSM-7 message, and in each part of a concatenated one.
#: The seven-septet difference is the UDH the parts carry so the handset can
#: reassemble them.
GSM7_SINGLE = 160
GSM7_CONCATENATED = 153

#: The same numbers for UCS-2, where the unit is a 16-bit code unit rather than
#: a septet: 140 octets per segment, less a 6-octet UDH on a concatenated part.
UCS2_SINGLE = 70
UCS2_CONCATENATED = 67


class Encoding:
    """The two encodings a GSM network will carry.

    Plain string constants rather than an enum: they are rendered into a
    template and compared against in a test, and there is no behaviour to hang
    off members.
    """

    GSM7 = "GSM-7"
    UCS2 = "UCS-2"


@dataclass(frozen=True)
class SegmentCount:
    """What one message body will cost to send.

    ``units`` is the count in the encoding's own unit — septets for GSM-7, 16-bit
    code units for UCS-2 — and is what the segment boundaries are actually drawn
    against. It is exposed alongside ``characters`` because the two differ, and
    a composer that shows only the character count cannot explain why a
    150-character message took two segments.
    """

    encoding: str
    characters: int
    units: int
    segments: int
    #: Units available in each segment *of this message* — the single-segment
    #: limit for a one-segment message, the concatenated one otherwise.
    limit: int

    @property
    def remaining(self) -> int:
        """Units left before this message needs another segment."""
        return max(0, self.segments * self.limit - self.units)


def segments_for(text: str) -> SegmentCount:
    """Count the segments ``text`` will be sent as.

    Matches Twilio's own arithmetic, which is the GSM 03.38 one: pick the
    encoding from the characters present, cost each character in that encoding's
    units, and pack. An empty string is one segment — a message is still a
    message — which is also what the API charges for.
    """
    if _is_gsm7(text):
        costs = [2 if char in _GSM7_EXTENDED_SET else 1 for char in text]
        return _count(text, costs, Encoding.GSM7, GSM7_SINGLE, GSM7_CONCATENATED)
    # A code point outside the BMP is a surrogate pair in UTF-16 and therefore
    # two units. ``len(char.encode("utf-16-le")) // 2`` says so without this
    # module having to know where the BMP ends.
    costs = [len(char.encode("utf-16-le")) // 2 for char in text]
    return _count(text, costs, Encoding.UCS2, UCS2_SINGLE, UCS2_CONCATENATED)


def _is_gsm7(text: str) -> bool:
    """Whether every character survives the GSM-7 alphabet.

    One character outside it re-encodes the **whole** message as UCS-2 — a
    single curly quote pasted from a word processor takes a 160-character
    message to 70 — which is the surprise this preview exists to remove.
    """
    return all(char in _GSM7_BASIC_SET or char in _GSM7_EXTENDED_SET for char in text)


def _count(text: str, costs: list[int], encoding: str, single: int, concatenated: int) -> SegmentCount:
    """Pack ``costs`` into segments, per character rather than by division.

    Dividing the total would split an escaped character or a surrogate pair
    across a boundary, which is not something the encoding permits — the pair
    moves whole into the next segment and the previous one goes out one unit
    short. See the module docstring.
    """
    units = sum(costs)
    if units <= single:
        return SegmentCount(encoding=encoding, characters=len(text), units=units, segments=1, limit=single)

    segments = 1
    used = 0
    for cost in costs:
        if used + cost > concatenated:
            segments += 1
            used = 0
        used += cost
    return SegmentCount(encoding=encoding, characters=len(text), units=units, segments=segments, limit=concatenated)
