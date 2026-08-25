"""SECURITY-BASELINE §6's "no exceptions", as a sweep rather than a promise.

``tests/ssrf.py::guard_required`` proves that *one* path went through the guard.
It cannot prove there is no *second* path, and the baseline's claim is about all
of them:

    Any server-initiated request to a user-supplied or contact-supplied URL goes
    through the SSRF guard … No exceptions.

So this counts the doors. Every HTTP request the product makes leaves through
one of exactly two functions — ``apps.common.outbound.guarded_request`` for a
URL somebody can influence, and ``apps.channels.providers.base.request_json``
for one an adapter builds from constants and stored ids — and a module that
issues its own is a call site nobody reasoned about.

Structural, over the AST, in the shape ``apps/messaging/tests/test_write_sites.py``
established: a docstring mentioning ``httpx.Client`` is not a request, and no
regex separates the two.
"""

import ast
from pathlib import Path

import pytest

APPS = Path(__file__).resolve().parents[1] / "apps"

#: HTTP verbs on a client object. ``get`` is in here because ``httpx.Client.get``
#: is real, which is also why the receiver has to be identified properly: a bare
#: name match on ``.get(`` finds several hundred dictionary reads per adapter.
ISSUING_METHODS = {"request", "stream", "send", "get", "post", "put", "patch", "delete", "head"}

#: The two doors, and what each is for.
ISSUANCE_SITES = {
    "common/outbound.py": (
        "The guard itself (SECURITY-BASELINE §6): resolves, validates every address, pins the "
        "connection to the literal, re-validates each redirect and caps the body."
    ),
    "channels/providers/base.py": (
        "request_json — the sanctioned sibling for a URL an adapter builds from constants and "
        "stored ids. Its own docstring argues why that is not an SSRF call site: 'An adapter that "
        "ever wants to fetch a contact- or user-supplied URL must use that guard instead.'"
    ),
}

#: Outbound machinery that never passes either door because it is not httpx,
#: with what constrains it instead. Named rather than skipped — "it is not
#: httpx" is not the same as "it is safe", and the audit says so out loud.
NON_HTTP_EGRESS = {
    "channels/providers/email_backends.py": (
        "boto3 to SES, and Django's SMTP backend to a workspace-configured host. Neither goes "
        "through httpx, so neither passes the guard. SMTP at least shares the guard's address "
        "rules as a pre-flight (refusal_for/resolve_host, pinned by the test below) but does not "
        "pin the connection, so DNS can move between the check and the connect; boto3 owns its "
        "transport entirely. Both are recorded PARTIAL against §6.1 in docs/security-audit.md, "
        "with filed follow-ups."
    ),
}


def _sources() -> list[Path]:
    return [p for p in APPS.rglob("*.py") if "migrations" not in p.parts and "tests" not in p.parts]


def _client_names(tree: ast.AST) -> set[str]:
    """Names in this module that hold an ``httpx`` client.

    Identifying the *receiver* is what makes the scan usable. Matching a bare
    ``.get(`` finds every dictionary read in the codebase — several hundred per
    adapter — and a scan whose output nobody can read is a scan nobody runs.

    Two bindings cover every real case: a name assigned from ``httpx.Client(...)``
    (possibly through an ``or``, as ``request_json`` does with its optional
    pooled client), and a parameter annotated ``httpx.Client``.
    """
    names: set[str] = set()

    def is_client_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Client", "AsyncClient"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "httpx"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = node.value
            candidates = value.values if isinstance(value, ast.BoolOp) else [value]
            if any(is_client_call(part) for part in candidates):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.withitem) and is_client_call(node.context_expr):
            if isinstance(node.optional_vars, ast.Name):
                names.add(node.optional_vars.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in [*node.args.args, *node.args.kwonlyargs]:
                if arg.annotation is not None and "Client" in ast.unparse(arg.annotation):
                    names.add(arg.arg)
    return names


def _issuing_modules() -> set[str]:
    """Modules that call an HTTP verb on something holding an httpx client."""
    found: set[str] = set()
    for path in _sources():
        text = path.read_text()
        if "httpx" not in text:
            continue
        tree = ast.parse(text)
        clients = _client_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ISSUING_METHODS:
                continue
            receiver = node.func.value
            module_level = isinstance(receiver, ast.Name) and receiver.id == "httpx"
            on_client = isinstance(receiver, ast.Name) and receiver.id in clients
            if module_level or on_client:
                found.add(str(path.relative_to(APPS)))
    return found


class TestEveryRequestLeavesThroughAKnownDoor:
    def test_only_two_modules_issue_http(self) -> None:
        assert _issuing_modules() == set(ISSUANCE_SITES), (
            "A module outside the two sanctioned doors issues an HTTP request. SECURITY-BASELINE §6 "
            "says any server-initiated request to a user- or contact-supplied URL goes through "
            "apps.common.outbound.guarded_request, with no exceptions; apps.channels.providers.base."
            "request_json is the sibling for adapter-built URLs. If this is neither, it wants the guard."
        )

    def test_no_other_http_library_is_imported(self) -> None:
        """httpx is the only client. ``requests`` bringing its own connection
        handling would bring its own redirect policy with it, and the guard's
        whole value is that it re-validates every hop."""
        offenders = []
        for path in _sources():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in {"requests", "urllib3"} or name.startswith("urllib.request"):
                        offenders.append(f"{path.relative_to(APPS)}: {name}")

        assert not offenders, "Another HTTP client was imported:\n  " + "\n  ".join(offenders)

    def test_non_http_egress_is_named_rather_than_forgotten(self) -> None:
        """boto3 bypasses both doors by being neither. That is a real hole in
        §6's "no exceptions", and it is recorded rather than rationalised."""
        importers = set()
        for path in _sources():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "boto3":
                    importers.add(str(path.relative_to(APPS)))

        assert importers <= set(NON_HTTP_EGRESS), (
            f"A module uses boto3 without being recorded as non-HTTP egress: "
            f"{sorted(importers - set(NON_HTTP_EGRESS))}. Record it in NON_HTTP_EGRESS and in "
            f"docs/security-audit.md §6."
        )

    def test_the_smtp_path_still_applies_the_guards_address_rules(self) -> None:
        """The weaker half of the SMTP story, pinned so it cannot get weaker.

        ``email_backends`` cannot use ``guarded_request`` — Django's SMTP
        backend owns the socket — but it imports the guard's own address
        predicates and runs them before connecting. If that pre-flight goes, the
        remaining protection is nothing at all, and the audit row would be a
        lie.
        """
        source = (APPS / "channels/providers/email_backends.py").read_text()

        assert "refusal_for" in source
        assert "resolve_host" in source

    def test_the_scan_would_catch_a_new_issuance_site(self) -> None:
        """A test that can only pass is not a test. Runs the real detector."""
        rogue = ast.parse(
            "import httpx\n\nclient = httpx.Client()\n\n\ndef go():\n    return client.post('http://x')\n"
        )

        clients = _client_names(rogue)
        issued = [
            node.func.attr
            for node in ast.walk(rogue)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ISSUING_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in clients
        ]

        assert clients == {"client"}
        assert issued == ["post"]

    def test_a_dictionary_read_is_not_an_issuance(self) -> None:
        """The false positive that made an earlier version of this unreadable:
        every adapter does hundreds of ``payload.get(...)``."""
        benign = ast.parse("import httpx\n\n\ndef go(payload):\n    return payload.get('x')\n")

        assert _client_names(benign) == set()


#: Every production call to ``guarded_request``, and the suite that proves the
#: guard was actually in the path for it.
#:
#: ``tests/ssrf.py`` opens by explaining why the proof has to be
#: ``guard_required()`` and not a patched ``guarded_request``:
#:
#:     Asserting that a patched ``guarded_request`` was called is not the same
#:     claim: it stays green when a second, unguarded request is made beside it.
#:
#: ``email_signatures.py`` was the one call site with no such proof — its tests
#: replaced the symbol — which issue #29 fixed by adding the module named here.
GUARD_PROOFS = {
    "api/delivery.py": "apps/api/tests/test_delivery.py",
    "channels/media.py": "apps/channels/tests/test_media.py",
    "channels/providers/email_signatures.py": "apps/channels/tests/test_email_signature_fetch.py",
    "flows/engine/nodes/external_request.py": "apps/flows/tests/test_node_external_request.py",
}


class TestEveryGuardedCallSiteIsProven:
    def test_the_table_lists_every_call_site(self) -> None:
        callers = set()
        for path in _sources():
            text = path.read_text()
            if "guarded_request" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "guarded_request":
                    callers.add(str(path.relative_to(APPS)))
        callers.discard("common/outbound.py")  # the guard defines it

        assert callers == set(GUARD_PROOFS), (
            f"guarded_request call sites changed: {sorted(callers ^ set(GUARD_PROOFS))}. "
            f"SECURITY-BASELINE §6: 'new call sites add a test proving the guard is in the path'."
        )

    @pytest.mark.parametrize("site,proof", sorted(GUARD_PROOFS.items()))
    def test_each_proof_uses_guard_required(self, site: str, proof: str) -> None:
        path = Path(__file__).resolve().parents[1] / proof

        assert path.exists(), f"{site} names {proof} as its proof, and it does not exist"
        assert "guard_required" in path.read_text(), (
            f"{proof} does not use guard_required(), so it does not prove {site} is guarded — "
            f"see the warning at the top of tests/ssrf.py."
        )


def test_the_ssrf_helper_itself_is_covered() -> None:
    """``guard_required`` has its own suite, and this file leans on it."""
    assert (Path(__file__).resolve().parents[1] / "tests" / "test_ssrf_helper.py").exists()


@pytest.mark.parametrize("module", sorted(ISSUANCE_SITES))
def test_each_door_still_exists(module: str) -> None:
    assert (APPS / module).exists()
