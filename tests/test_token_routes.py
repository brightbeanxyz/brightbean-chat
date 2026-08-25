"""Public token routes, swept rather than remembered (SECURITY-BASELINE §4, §5).

``apps/common/logging.py`` carries a list of URL prefixes whose next path
segment is a bearer credential, so the scrubber can redact it out of a request
line. The comment above it says "extend this when a new unauthenticated token
route lands" — and issue #16's ``/m/`` landed without anybody extending it, so
every media-delivery request had been writing a live capability into the access
log. The token *is* the authorisation there: ``apps/media_library/views.py``
reads it and then queries ``unscoped()``.

A list maintained by remembering is a list that goes stale, so this derives the
routes from the URL conf and compares. The same shape ``tests/idor.py`` uses for
tenant routes, and for the same reason: the route nobody remembers is exactly
the one that needed remembering.
"""

import re
from pathlib import Path

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from apps.common.logging import _TOKEN_PATH_PREFIXES, REDACTED, scrub

ROOT = Path(__file__).resolve().parents[1]

#: Routes whose ``token`` kwarg is *not* a bearer credential, with the reason.
#: Empty today. An entry here is a claim that holding the token grants nothing,
#: which for a route named ``<str:token>`` needs saying out loud.
NOT_A_CREDENTIAL: dict[str, str] = {}


def token_routes() -> dict[str, str]:
    """``{route name: first path segment}`` for every route taking a ``token``.

    Walks the URL conf rather than reading a list, which is the whole point.
    """
    found: dict[str, str] = {}

    def walk(resolver: URLResolver, prefix: str) -> None:
        for entry in resolver.url_patterns:
            if isinstance(entry, URLResolver):
                walk(entry, prefix + str(entry.pattern))
                continue
            if not isinstance(entry, URLPattern):  # pragma: no cover - defensive
                continue
            route = prefix + str(entry.pattern)
            if "<str:token>" not in route and "<token>" not in route:
                continue
            found[entry.name or route] = route.lstrip("/").split("/", 1)[0]

    walk(get_resolver(None), "")
    return found


class TestEveryTokenRouteIsScrubbedFromLogs:
    def test_the_prefix_list_covers_every_token_route(self) -> None:
        """The assertion that would have caught ``/m/``."""
        declared = set(_TOKEN_PATH_PREFIXES.split("|"))
        actual = {segment for name, segment in token_routes().items() if name not in NOT_A_CREDENTIAL}

        missing = actual - declared
        assert not missing, (
            f"These token routes are not scrubbed from request lines: {sorted(missing)}. "
            f"Add the prefix to apps/common/logging.py::_TOKEN_PATH_PREFIXES, or add the route to "
            f"NOT_A_CREDENTIAL here with the reason holding its token grants nothing."
        )

    def test_no_prefix_is_stale(self) -> None:
        """The other direction. A prefix for a route that no longer exists is a
        pattern redacting something nobody serves."""
        declared = set(_TOKEN_PATH_PREFIXES.split("|"))
        actual = {segment for segment in token_routes().values() if segment}

        assert declared <= actual, f"Prefixes with no route: {sorted(declared - actual)}"

    @pytest.mark.parametrize("prefix", sorted(_TOKEN_PATH_PREFIXES.split("|")))
    def test_a_request_line_is_redacted(self, prefix: str) -> None:
        """End to end, in the shape a log actually carries."""
        line = f'GET /{prefix}/eyJ2IjoxfQ.aBcDeFgHiJkLmNoP1234567890 HTTP/1.1" 200'

        cleaned = scrub(line)

        assert "aBcDeFgHiJkLmNoP" not in cleaned
        assert REDACTED in cleaned

    def test_an_unlisted_prefix_really_does_leak(self) -> None:
        """A test that can only pass is not a test.

        This is the failure the sweep above exists to prevent, demonstrated: a
        token route whose prefix is not in the list has its credential written
        to the log in full. It is what ``/m/`` was doing until this issue.
        """
        line = "GET /notregistered/eyJ2IjoxfQ.aBcDeFgHiJkLmNoP1234567890 HTTP/1.1"

        assert "aBcDeFgHiJkLmNoP" in scrub(line)


@pytest.mark.django_db
class TestEveryTokenRouteAnswersABare404:
    """SECURITY-BASELINE §4: "any failure returns a generic 404 (no error
    detail, no timing oracle)"."""

    @pytest.mark.parametrize("prefix", ["u", "m", "invite"])
    def test_a_garbage_token_is_indistinguishable_from_a_missing_one(self, client: Client, prefix: str) -> None:
        forged = client.get(f"/{prefix}/not-a-real-token/")
        unknown = client.get(f"/{prefix}/{'a' * 40}/")

        assert forged.status_code == 404
        assert unknown.status_code == 404
        # Neither body says which kind of wrong it was — the invitation page
        # deliberately reads "invalid or expired" for unknown, expired *and*
        # accepted, which is the property rather than a leak. Compared by length
        # rather than byte-for-byte: the 404 page carries a per-request CSP
        # nonce, so two renderings of the *same* page never match exactly, and
        # an equality assertion here could only ever fail.
        assert len(forged.content) == len(unknown.content)
        # And neither echoes the token it was given, which is the one thing that
        # would differ between the two and the one thing a log or a referrer
        # header would then carry onward.
        assert b"not-a-real-token" not in forged.content
        assert b"aaaaaaaa" not in unknown.content


class TestNoCredentialIsComparedWithEquality:
    """SECURITY-BASELINE §4: "verification is constant-time"."""

    #: Modules that verify a presented credential, and the helper each uses.
    VERIFIERS = {
        "apps/common/signing.py": "hmac.compare_digest",
        "apps/queueing/views.py": "constant_time_compare",
        "apps/channels/security.py": "constant_time_equal",
        "apps/api/keys.py": "compare_digest",
    }

    @pytest.mark.parametrize("module,helper", sorted(VERIFIERS.items()))
    def test_the_verifier_uses_a_constant_time_helper(self, module: str, helper: str) -> None:
        source = (ROOT / module).read_text()

        assert helper.split(".")[-1] in source, f"{module} no longer references {helper}"

    def test_no_verifier_compares_a_digest_with_a_plain_operator(self) -> None:
        """A grep would report every ``==`` in the file; this narrows to the
        names that hold credential material."""
        pattern = re.compile(r"\b(?:signature|digest|secret|token)\w*\s*==\s*(?!=)", re.IGNORECASE)
        offenders: list[str] = []
        for module in self.VERIFIERS:
            for number, line in enumerate((ROOT / module).read_text().splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#") or not pattern.search(line):
                    continue
                offenders.append(f"{module}:{number}: {stripped}")

        assert not offenders, "Credential compared with ==:\n  " + "\n  ".join(offenders)
