"""Bounding an untrusted JSON document before a parser recurses through it.

SECURITY-BASELINE §7: "JSON documents authored by users […] get size + depth
caps". Two places take JSON from a stranger and neither can afford to learn how
deep it is by parsing it — inbound webhook payloads (:mod:`apps.channels.security`,
issue #4) and outbound responses fetched through the SSRF guard
(:mod:`apps.common.outbound`, issue #15).

It lives here rather than in either of them because it is a property of
untrusted JSON, not of webhooks or of outbound HTTP, and because
``apps.common`` is the only place both can import from without an app depending
on a sibling app.
"""

__all__ = ["DEFAULT_MAX_JSON_DEPTH", "max_json_depth"]

#: Nesting past this is a bomb, not a payload. Meta's deepest real structure is
#: about six levels; a REST API's response is rarely past ten.
DEFAULT_MAX_JSON_DEPTH = 20


def max_json_depth(raw: bytes) -> int:
    """Deepest bracket nesting in ``raw``, without parsing it.

    A linear scan over bytes, string-aware so a ``{`` inside a quoted value does
    not count. Deliberately done **before** ``json.loads``: Python's parser
    recurses, and a deeply nested document raises ``RecursionError`` at best and
    overflows the C stack at worst — so the cap has to apply to the input rather
    than to the result. Catching the exception afterwards is not equivalent,
    because whether there is an exception to catch depends on how much stack the
    caller had left.
    """
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # closing quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            deepest = max(deepest, depth)
        elif byte in (0x7D, 0x5D):  # } ]
            depth -= 1
    return deepest
