"""The deployment configurations stay secure by default (issue #28).

`docs/self-hosting.md` makes promises on behalf of four files nothing else in
this repository reads: `docker-compose.prod.yml`,
`deploy/docker-compose.external-tls.yml`, `deploy/Caddyfile` and
`deploy/env.prod.example`. A regression in any of them is invisible to the
running stack: it still boots, while the deployment is less safe than the guide
says. CI's `build` job proves the compose stack works end to end; these assert
the properties that would still be true of a working-but-weakened one.

Each test says which promise it is holding. The expensive ones — that the stack
actually starts, that the headers actually arrive, that the database is actually
unreachable — belong to `scripts/smoke.sh` and the `build` job, not here.
"""

import ipaddress
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from apps.common.placeholders import is_placeholder_secret

REPO_ROOT = Path(__file__).resolve().parents[1]

PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
EXTERNAL_TLS_COMPOSE = REPO_ROOT / "deploy" / "docker-compose.external-tls.yml"
CADDYFILE = REPO_ROOT / "deploy" / "Caddyfile"
ENV_TEMPLATE = REPO_ROOT / "deploy" / "env.prod.example"
SELF_HOSTING = REPO_ROOT / "docs" / "self-hosting.md"

#: The two values that decrypt a database dump. Both must be generated once and
#: shared by every process — see the Railway section below for why this is
#: asserted against prose rather than against a configuration file.
CRYPTO_SECRETS = ("SECRET_KEY", "ENCRYPTION_KEY_SALT")

#: Settings a split web/worker deployment is broken without, and which the
#: compose stack gets for free because both services read one `.env`.
#:
#: ``TRUSTED_PROXIES``: apps.common.net.get_client_ip returns REMOTE_ADDR unless
#: the peer is trusted, and on a PaaS the peer is always the platform router.
#: Left unset, auth rate limiting, the API auth-failure throttle and the webhook
#: signature ban all attribute every request to that one address.
#:
#: ``STORAGE_BACKEND`` + ``S3_*``: a CSV contact import is written by the web
#: process (apps/contacts/views.py) and opened by the worker
#: (apps/contacts/imports.py). Compose gives both the media_data volume;
#: separate PaaS services share no filesystem at all.
SPLIT_PROCESS_SETTINGS = (
    "TRUSTED_PROXIES",
    "STORAGE_BACKEND",
    "S3_BUCKET_NAME",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_ENDPOINT_URL",
    "S3_REGION_NAME",
)

#: Services built from the application image, which therefore need production
#: settings and the same environment. `caddy` and `postgres` are third-party
#: images and are exempt.
APP_SERVICES = ("migrate", "app", "worker")

#: Values a deployment must supply before anything starts. SECURITY-BASELINE §8:
#: "Production settings refuse to boot without SECRET_KEY + ENCRYPTION_KEY_SALT."
#: The other three are the ones that make *this* stack a deployment rather than a
#: template — the hostname it answers on, the database password, and the address
#: the certificate is registered to.
REQUIRED_VARIABLES = (
    "SECRET_KEY",
    "ENCRYPTION_KEY_SALT",
    "POSTGRES_PASSWORD",
    "APP_DOMAIN",
    "ACME_EMAIL",
)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict), f"{path.name} did not parse as a mapping"
    return loaded


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return _load_yaml(PROD_COMPOSE)


@pytest.fixture(scope="module")
def compose_source() -> str:
    """The raw text.

    The parsed document is the right tool for structure, but `${VAR:?message}`
    is a *string* until compose interpolates it, and interpolation is exactly
    what these tests are checking is still there.
    """
    return PROD_COMPOSE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# docker-compose.prod.yml
# ---------------------------------------------------------------------------


def test_the_production_compose_parses(compose: dict[str, Any]) -> None:
    """A syntax error here is only discovered by someone trying to deploy."""
    assert set(APP_SERVICES) | {"postgres", "caddy"} == set(compose["services"])


@pytest.mark.parametrize("variable", REQUIRED_VARIABLES)
def test_every_required_value_aborts_the_stack_when_missing(compose_source: str, variable: str) -> None:
    """No default, and a message that names the variable.

    `${VAR:?message}` makes `docker compose up` exit before it pulls an image.
    Softening one of these to `${VAR:-something}` would let a deployment start
    with a value the operator never chose — which for SECRET_KEY or
    ENCRYPTION_KEY_SALT means signing sessions and encrypting credentials with a
    string that is not a secret (SECURITY-BASELINE §8).
    """
    assert f"${{{variable}:?" in compose_source, (
        f"{variable} no longer uses the ${{{variable}:?message}} form, so the stack would "
        f"start without it instead of refusing to"
    )


def test_every_refusal_says_where_to_look(compose_source: str) -> None:
    """Each message has to be self-sufficient, because only one is ever shown.

    Compose stops at the FIRST variable it cannot interpolate, and which one
    that is depends on document order rather than on what the operator forgot.
    A message that assumed a previous one had already been read would leave them
    with a bare variable name and nowhere to go.
    """
    directives = "\n".join(line for line in compose_source.splitlines() if not line.lstrip().startswith("#"))
    messages = re.findall(r"\$\{([A-Z_]+):\?([^}]*)\}", directives)
    assert messages
    for name, message in messages:
        assert "deploy/env.prod.example" in message, f"{name}'s refusal does not say where to look"


def test_postgres_is_not_published(compose: dict[str, Any]) -> None:
    """Not even on loopback.

    A loopback publish is still reachable from every other container on the host
    and from anyone who can open an SSH tunnel, and this database holds contacts
    and the encrypted platform credentials. The development stack publishes on
    127.0.0.1 deliberately; this one publishes nothing.
    """
    assert "ports" not in compose["services"]["postgres"]


def test_only_the_proxy_publishes_ports(compose: dict[str, Any]) -> None:
    """The app is reachable only through the thing that adds TLS."""
    publishing = sorted(name for name, service in compose["services"].items() if service.get("ports"))
    assert publishing == ["caddy"]


@pytest.mark.parametrize("service", APP_SERVICES)
def test_every_app_service_runs_production_settings(compose: dict[str, Any], service: str) -> None:
    """Including `migrate`.

    A one-shot that ran development settings would migrate happily against the
    hardcoded, repo-public SECRET_KEY — and any encrypted column it wrote would
    be unreadable by the two services that do not.
    """
    environment = compose["services"][service]["environment"]
    assert environment["DJANGO_SETTINGS_MODULE"] == "config.settings.production"


@pytest.mark.parametrize("service", APP_SERVICES)
def test_the_environment_is_the_only_source_of_configuration(compose: dict[str, Any], service: str) -> None:
    """`DJANGO_ENV_FILE=/nonexistent`, so a stray .env cannot disagree with it."""
    assert compose["services"][service]["environment"]["DJANGO_ENV_FILE"] == "/nonexistent"


def test_no_service_turns_debug_on(compose: dict[str, Any]) -> None:
    """`config.settings.production` forces DEBUG off, and nothing here asks for it.

    The settings module is the real guard — it sets DEBUG before importing
    anything, precisely so the environment cannot unlock it. This catches the
    change that makes someone *think* it can.
    """
    for name, service in compose["services"].items():
        environment = service.get("environment") or {}
        assert "DEBUG" not in environment, f"{name} sets DEBUG"


def test_the_app_runs_gunicorn_with_the_documented_concurrency(compose: dict[str, Any]) -> None:
    """SPEC §20: "app (gunicorn, 4 workers 2 threads)"."""
    command = compose["services"]["app"]["command"]
    assert command[0] == "gunicorn"
    assert command[command.index("--workers") + 1] == "4"
    assert command[command.index("--threads") + 1] == "2"


def test_the_app_does_not_log_query_strings(compose: dict[str, Any]) -> None:
    """No `--access-logfile`.

    gunicorn's access log records the full request line, and both
    `/internal/tick?token=…` and Meta's `hub.verify_token` travel in a query
    string. The application log is scrubbed (SECURITY-BASELINE §5); gunicorn's
    is not.
    """
    assert "--access-logfile" not in compose["services"]["app"]["command"]


def test_the_worker_runs_the_queue(compose: dict[str, Any]) -> None:
    """Without this the deployment looks healthy and fires nothing time-based."""
    assert compose["services"]["worker"]["command"] == ["python", "manage.py", "process_tasks"]


def test_the_migration_is_a_one_shot(compose: dict[str, Any]) -> None:
    """`restart: unless-stopped` here would re-run the migration forever."""
    assert compose["services"]["migrate"]["restart"] == "no"
    for service in ("app", "worker"):
        assert compose["services"][service]["depends_on"]["migrate"] == {"condition": "service_completed_successfully"}


def test_the_health_probe_presents_a_host_django_will_accept(compose: dict[str, Any]) -> None:
    """The probe connects to 127.0.0.1 but must not send it as the Host header.

    A production ALLOWED_HOSTS does not list 127.0.0.1, so a probe that sends it
    gets 400 DisallowedHost, the container never becomes healthy, caddy never
    starts because it waits on the app, and nothing in the output says why. The
    fix is a Host header taken from APP_URL — not a wider ALLOWED_HOSTS.
    """
    probe = " ".join(compose["services"]["app"]["healthcheck"]["test"])
    assert "'Host'" in probe and "APP_URL" in probe


@pytest.mark.parametrize("service", ("postgres", "caddy"))
def test_third_party_images_are_pinned(compose: dict[str, Any], service: str) -> None:
    """A floating `latest` makes a rebuild a different deployment."""
    image = compose["services"][service]["image"]
    _, _, tag = image.partition(":")
    assert tag and tag != "latest", f"{service} runs {image}"


def test_privilege_escalation_is_disabled_everywhere(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        assert "no-new-privileges:true" in service.get("security_opt", []), f"{name} allows privilege escalation"


@pytest.mark.parametrize("service", APP_SERVICES)
def test_app_containers_hold_no_capabilities(compose: dict[str, Any], service: str) -> None:
    """The image already runs as uid 1001 and binds an unprivileged port."""
    assert compose["services"][service]["cap_drop"] == ["ALL"]


def test_the_worker_shares_the_media_volume_with_the_app(compose: dict[str, Any]) -> None:
    """This is what makes STORAGE_BACKEND=local correct on the compose stack.

    A CSV contact import is written by the web process and opened by the worker.
    Drop this mount and queued imports fail with a missing file, while every
    other thing the worker does keeps working — so it reads as an import bug
    rather than as a deployment one.
    """
    for service in ("app", "worker"):
        mounts = compose["services"][service]["volumes"]
        assert any(str(mount).endswith(":/app/media") for mount in mounts), service


def test_the_external_tls_override_never_publishes_the_app_publicly() -> None:
    """Loopback only.

    `0.0.0.0:8000` here would put an app that believes it is behind TLS — HSTS,
    secure cookies, the lot — directly on the internet over plain HTTP.
    """
    override = _load_yaml(EXTERNAL_TLS_COMPOSE)
    for published in override["services"]["app"]["ports"]:
        assert str(published).startswith("127.0.0.1:"), published
    # And Caddy is excluded rather than left running with nothing to do.
    assert override["services"]["caddy"]["profiles"]


# ---------------------------------------------------------------------------
# deploy/Caddyfile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"),
)
def test_the_proxy_sets_every_required_header(header: str) -> None:
    """SECURITY-BASELINE §8 requires all four at the proxy, and smoke.sh checks them."""
    assert header in CADDYFILE.read_text(encoding="utf-8")


def test_the_header_block_is_deferred() -> None:
    """Without `defer` the header operations run before reverse_proxy answers.

    Caddy applies them to the response header map first, then copies the
    upstream headers over the top — so the app's values win and `-Server`
    deletes a header that has not arrived yet. The block would look correct and
    do nothing of its own.
    """
    source = CADDYFILE.read_text(encoding="utf-8")
    header_block = source[source.index("header {") :]
    assert "defer" in header_block[: header_block.index("}")]


def test_the_proxy_does_not_set_a_content_security_policy() -> None:
    """Django owns the CSP, because only Django can put the nonce in it.

    A static copy at the edge could not carry the per-request nonce
    (SECURITY-BASELINE §8) and would break every page it "protected". Comments
    are stripped first: the Caddyfile explains this decision in prose, and the
    explanation is not a directive.
    """
    directives = [
        line for line in CADDYFILE.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("#")
    ]
    assert "content-security-policy" not in "\n".join(directives).lower()


# ---------------------------------------------------------------------------
# deploy/env.prod.example
# ---------------------------------------------------------------------------


def _template_assignments() -> dict[str, str]:
    """The uncommented `KEY=value` lines of the production template."""
    assignments = {}
    for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if match:
            assignments[match.group(1)] = match.group(2)
    return assignments


@pytest.mark.parametrize("variable", REQUIRED_VARIABLES)
def test_the_template_names_every_required_variable(variable: str) -> None:
    """Otherwise the operator meets the requirement as an error rather than a field."""
    assert variable in _template_assignments()


def test_the_template_leaves_every_required_value_empty() -> None:
    """Empty, not a plausible-looking stand-in.

    `.env.example` ships `change-me-…` values, which `make setup` copies and
    `apps.common.placeholders` exists to reject. The production template avoids
    the question for the values that matter: an unset variable fails with the
    settings module's own hint, which names it and how to generate it.

    Scoped to the required variables rather than to every assignment. The
    earlier, wider form said "nothing in this file may have a value", which is
    the right rule for a secret and the wrong one for a documented default — it
    made adding `STORAGE_BACKEND=local` to the template fail the suite, and so
    pushed exactly the settings an operator needs to find into comments.
    """
    assignments = _template_assignments()
    for name in REQUIRED_VARIABLES:
        assert assignments[name] == "", f"{name} has a value in the production template"


def test_the_template_ships_no_placeholder_secret() -> None:
    """A real default is fine; a convincing stand-in for a secret is not."""
    for name, value in _template_assignments().items():
        assert not is_placeholder_secret(value), f"{name} is a placeholder: {value!r}"


@pytest.mark.parametrize("variable", ("STORAGE_BACKEND", "IMAGE_REPOSITORY", "APP_BIND_PORT"))
def test_the_template_documents_every_knob_the_deploy_files_read(variable: str) -> None:
    """Commented or not, the operator has to be able to find it.

    Each of these changes what the stack does and is read by a file in this
    directory, so a template that omits it is a setting discoverable only by
    reading the compose source.
    """
    body = ENV_TEMPLATE.read_text(encoding="utf-8")
    assert re.search(rf"^#?\s*{variable}=", body, re.MULTILINE), f"{variable} is undocumented"


# ---------------------------------------------------------------------------
# The Railway setup, which lives in prose
# ---------------------------------------------------------------------------
#
# Railway is configured in its dashboard and in a template this repository does
# not contain, so there is no file to assert against the way `app.json` and
# `render.yaml` once were. What is left is the procedure `docs/self-hosting.md`
# tells an operator to follow, and these hold that procedure to the same
# split-process invariants the deleted blueprint tests held.
#
# This is genuinely weaker: it proves the guide still says the right thing, not
# that any deployment does it. It is here because the alternative — after the
# Heroku and Render blueprints were removed — is no check at all on the one
# remaining PaaS target, and the failure it guards is silent and expensive.


def _railway_variables() -> dict[str, tuple[str, str]]:
    """``{key cell: (web cell, worker cell)}`` from Railway → Variables."""
    section = SELF_HOSTING.read_text(encoding="utf-8").partition("\n### Variables\n")[2].partition("\n### ")[0]
    assert section.strip(), "docs/self-hosting.md has no Railway 'Variables' section"

    rows = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] == "Variable" or set(cells[0]) <= {"-"}:
            continue
        rows[cells[0]] = (cells[1], cells[2])
    assert rows, "the Railway variables table parsed to nothing"
    return rows


def _railway_row(variable: str) -> tuple[str, str]:
    match = next((cells for key, cells in _railway_variables().items() if variable in key), None)
    assert match is not None, f"the Railway variables table does not mention {variable}"
    return match


@pytest.mark.parametrize("variable", CRYPTO_SECRETS)
def test_the_railway_worker_takes_its_crypto_secrets_from_the_web_service(variable: str) -> None:
    """The bug the deleted blueprint tests existed to prevent.

    Two services that each generate their own value deploy green and stay green:
    nothing fails until the worker tries to read a channel credential the web
    process encrypted, days later, as one broken feature. The guide must tell
    the operator to *reference* web's value, never to generate a second one.
    """
    _web, worker = _railway_row(variable)

    assert "${{web." + variable + "}}" in worker, (
        f"the Railway guide no longer tells the worker to reference "
        f"${{{{web.{variable}}}}}. A worker that generates its own {variable} "
        f"cannot decrypt anything the web process wrote."
    )


def test_the_railway_guide_puts_both_services_on_s3() -> None:
    """A Railway volume attaches to one service, so `local` is not an option."""
    web, worker = _railway_row("STORAGE_BACKEND")

    assert "s3" in web and "s3" in worker, f"STORAGE_BACKEND is not s3 on both services: {web!r} / {worker!r}"
    assert "local" not in web and "local" not in worker, (
        "the Railway guide offers STORAGE_BACKEND=local, which loses uploaded "
        "media on restart and breaks queued contact imports"
    )


def test_the_railway_guide_covers_every_split_process_setting() -> None:
    """Each of these is a setting the compose stack gets for free and a split
    deployment does not. A missing row is a deployment that looks healthy."""
    keys = " ".join(_railway_variables())
    missing = [
        name
        for name in SPLIT_PROCESS_SETTINGS
        # The table covers the bucket credentials with one `S3_*` row.
        if name not in keys and not (name.startswith("S3_") and "S3_*" in keys)
    ]

    assert not missing, f"the Railway variables table documents no value for: {missing}"


def test_the_railway_guide_trusts_only_private_ranges_for_client_addresses() -> None:
    """Without this the platform router is the client for every request, and
    auth rate limiting, API throttling and the webhook signature ban all
    collapse into one shared bucket — one caller can throttle everybody."""
    web, _worker = _railway_row("TRUSTED_PROXIES")

    entries = [entry.strip() for entry in web.strip("`").split(",")]
    assert entries, "TRUSTED_PROXIES is documented with no value"
    for entry in entries:
        assert ipaddress.ip_network(entry, strict=False).is_private, (
            f"{entry} is publicly routable; a client could present it and be believed"
        )


# ---------------------------------------------------------------------------
# The documentation these files are described by
# ---------------------------------------------------------------------------

DOCUMENTED = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "docs" / "self-hosting.md",
)

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("document", DOCUMENTED, ids=lambda path: path.name)
def test_every_repository_link_resolves(document: Path) -> None:
    """A deployment guide that links to a file that moved is worse than no guide.

    Only repository-relative links are followed; external URLs and bare anchors
    are somebody else's problem.
    """
    broken = []
    for target in _MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path, _, _anchor = target.partition("#")
        if not path:
            continue
        if not (document.parent / path).resolve().exists():
            broken.append(target)
    assert not broken, f"{document.name} links to files that do not exist: {broken}"
