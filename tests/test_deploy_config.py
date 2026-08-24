"""The deployment configurations stay secure by default (issue #28).

`docs/self-hosting.md` makes promises on behalf of five files nothing else in
this repository reads: `docker-compose.prod.yml`, `deploy/Caddyfile`,
`deploy/env.prod.example`, `app.json`, `render.yaml` and the two Railway
configs. A regression in any of them is invisible — the stack still boots, the
blueprint still validates, and the deployment is quietly less safe than the
guide says it is. CI's `build` job proves the compose stack works end to end;
these assert the properties that would still be true of a working-but-weakened
one.

Each test says which promise it is holding. The expensive ones — that the stack
actually starts, that the headers actually arrive, that the database is actually
unreachable — belong to `scripts/smoke.sh` and the `build` job, not here.
"""

import ipaddress
import json
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
APP_JSON = REPO_ROOT / "app.json"
RENDER_YAML = REPO_ROOT / "render.yaml"
RAILWAY_WEB = REPO_ROOT / "railway.json"
RAILWAY_WORKER = REPO_ROOT / "deploy" / "railway.worker.json"

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

#: The two values that decrypt a database dump. Both must be generated once and
#: shared by every process, which is what the PaaS blueprints are checked for.
CRYPTO_SECRETS = ("SECRET_KEY", "ENCRYPTION_KEY_SALT")

#: Settings a split web/worker deployment is broken without, and which the
#: compose stack gets for free — so only the PaaS blueprints are checked.
#:
#: ``TRUSTED_PROXIES``: apps.common.net.get_client_ip returns REMOTE_ADDR unless
#: the peer is trusted, and on a PaaS the peer is always the platform router. Left
#: unset, auth rate limiting, the API auth-failure throttle and the webhook
#: signature ban all attribute every request to that one address.
#:
#: ``STORAGE_BACKEND`` + ``S3_*``: a CSV contact import is written by the web
#: process (apps/contacts/views.py) and opened by the worker
#: (apps/contacts/imports.py). Compose gives both the media_data volume; separate
#: PaaS processes share no filesystem at all.
SPLIT_PROCESS_SETTINGS = (
    "TRUSTED_PROXIES",
    "STORAGE_BACKEND",
    "S3_BUCKET_NAME",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_ENDPOINT_URL",
    "S3_REGION_NAME",
)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict), f"{path.name} did not parse as a mapping"
    return loaded


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    assert isinstance(loaded, dict), f"{path.name} did not parse as an object"
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
# app.json (Heroku)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_json() -> dict[str, Any]:
    return _load_json(APP_JSON)


@pytest.mark.parametrize("variable", CRYPTO_SECRETS)
def test_heroku_generates_its_secrets(app_json: dict[str, Any], variable: str) -> None:
    """`generator: secret`, never a literal — a committed key is not a secret."""
    entry = app_json["env"][variable]
    assert entry.get("generator") == "secret"
    assert "value" not in entry


def test_heroku_runs_production_settings(app_json: dict[str, Any]) -> None:
    env = app_json["env"]
    assert env["DJANGO_SETTINGS_MODULE"]["value"] == "config.settings.production"
    assert env["DJANGO_ENV_FILE"]["value"] == "/nonexistent"
    assert "DEBUG" not in env


def test_heroku_asks_for_its_hostname_rather_than_guessing(app_json: dict[str, Any]) -> None:
    """A wildcard would be a Host-header attack against every link the app builds."""
    for variable in ("ALLOWED_HOSTS", "APP_URL"):
        entry = app_json["env"][variable]
        assert entry.get("required") is True
        assert "value" not in entry
        assert entry.get("description")


def test_heroku_runs_a_worker(app_json: dict[str, Any]) -> None:
    """Both dynos, and neither on a plan that sleeps.

    An Eco dyno stops after 30 minutes of inactivity: a sleeping web dyno drops
    the webhook that would have woken it, and a sleeping worker is no worker.
    """
    formation = app_json["formation"]
    assert set(formation) == {"web", "worker"}
    for process, spec in formation.items():
        assert spec["quantity"] >= 1
        assert spec["size"] != "eco", process


def test_heroku_provisions_postgres(app_json: dict[str, Any]) -> None:
    plans = [addon["plan"] if isinstance(addon, dict) else addon for addon in app_json["addons"]]
    assert any(plan.startswith("heroku-postgresql") for plan in plans)


def test_the_node_buildpack_runs_before_the_python_one(app_json: dict[str, Any]) -> None:
    """Order is load-bearing.

    The Python buildpack runs `collectstatic`, and production uses
    CompressedManifestStaticFilesStorage, which hard-fails on a `{% static %}`
    reference it cannot resolve. The Tailwind bundle and the flow-builder island
    have to exist by then, and the Node buildpack is what builds them.
    """
    urls = [buildpack["url"] for buildpack in app_json["buildpacks"]]
    assert urls.index("heroku/nodejs") < urls.index("heroku/python")


@pytest.mark.parametrize("variable", SPLIT_PROCESS_SETTINGS)
def test_heroku_configures_the_split_process_settings(app_json: dict[str, Any], variable: str) -> None:
    """A web dyno and a worker dyno share a database and nothing else."""
    assert variable in app_json["env"]


def test_heroku_trusts_its_router_for_client_addresses(app_json: dict[str, Any]) -> None:
    """Set, and set to private ranges only.

    A public client can never present a private REMOTE_ADDR, so trusting those
    peers cannot be abused from the internet — whereas a public range here would
    let a caller forge X-Forwarded-For and evade the limiters entirely.
    """
    value = app_json["env"]["TRUSTED_PROXIES"]["value"]
    assert value
    for entry in value.split(","):
        assert ipaddress.ip_network(entry.strip(), strict=False).is_private, entry


# ---------------------------------------------------------------------------
# render.yaml
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def render() -> dict[str, Any]:
    return _load_yaml(RENDER_YAML)


@pytest.mark.parametrize("variable", CRYPTO_SECRETS)
def test_render_shares_one_generated_key_between_both_services(render: dict[str, Any], variable: str) -> None:
    """The bug this file's shape exists to prevent.

    `generateValue: true` generates a DIFFERENT value per service it is written
    on. Written on the web service and the worker separately, each would get its
    own SECRET_KEY and ENCRYPTION_KEY_SALT — and every platform credential the
    worker encrypted would be undecryptable by the web process. It deploys
    green and fails on the first channel connection.
    """
    groups = {group["name"]: group for group in render["envVarGroups"]}
    owning = [
        name
        for name, group in groups.items()
        if any(var.get("key") == variable and var.get("generateValue") for var in group["envVars"])
    ]
    assert len(owning) == 1, f"{variable} is not generated in exactly one env group: {owning}"

    for service in render["services"]:
        keys = [var.get("key") for var in service["envVars"]]
        assert variable not in keys, f"{service['name']} declares {variable} instead of sharing the group's"
        assert owning[0] in [var.get("fromGroup") for var in service["envVars"]], service["name"]


def test_render_runs_a_web_service_and_a_worker(render: dict[str, Any]) -> None:
    services = {service["type"]: service for service in render["services"]}
    assert set(services) == {"web", "worker"}
    assert services["web"]["healthCheckPath"] == "/healthz"
    assert "migrate" in services["web"]["preDeployCommand"]
    assert services["worker"]["dockerCommand"] == "python manage.py process_tasks"


def test_render_runs_production_settings(render: dict[str, Any]) -> None:
    for service in render["services"]:
        values = {var["key"]: var.get("value") for var in service["envVars"] if "key" in var}
        assert values["DJANGO_SETTINGS_MODULE"] == "config.settings.production"
        assert values["DJANGO_ENV_FILE"] == "/nonexistent"
        assert "DEBUG" not in values
        # Prompted, not defaulted: a blueprint cannot know the hostname, and
        # guessing it with a wildcard is the failure mode.
        assert values["ALLOWED_HOSTS"] is None
        assert values["APP_URL"] is None


def test_the_render_database_is_not_open_to_the_internet(render: dict[str, Any]) -> None:
    """An empty allow list closes it to everything but Render services.

    Omitting the key entirely is what leaves it reachable from any address.
    """
    for database in render["databases"]:
        assert database["ipAllowList"] == []


@pytest.mark.parametrize("variable", SPLIT_PROCESS_SETTINGS)
def test_render_configures_the_split_process_settings_on_both(render: dict[str, Any], variable: str) -> None:
    """Both services, not just the web one.

    The worker is the process that opens a contact-import file and the one whose
    outbound deliveries are rate limited, so a setting present only on the web
    service fixes the half of the problem that was easiest to see.
    """
    for service in render["services"]:
        keys = [var.get("key") for var in service["envVars"] if "key" in var]
        assert variable in keys, f"{service['name']} is missing {variable}"


def test_render_trusts_its_router_for_client_addresses(render: dict[str, Any]) -> None:
    """Private ranges only, and identical on both services."""
    values = set()
    for service in render["services"]:
        value = next(var["value"] for var in service["envVars"] if var.get("key") == "TRUSTED_PROXIES")
        values.add(value)
        for entry in value.split(","):
            assert ipaddress.ip_network(entry.strip(), strict=False).is_private, entry
    assert len(values) == 1, f"the services disagree about TRUSTED_PROXIES: {values}"


def test_render_lets_the_storage_switch_survive_a_sync(render: dict[str, Any]) -> None:
    """`value:` here would revert the operator's choice on the next deploy.

    Render re-applies a blueprint `value` on every sync and ignores `sync: false`
    entries after the first. Pinned to `local`, a switch to `s3` made in the
    dashboard would silently come back as `local` — and queued contact imports
    would start failing again in a way that reads as an import bug.
    """
    for service in render["services"]:
        entry = next(var for var in service["envVars"] if var.get("key") == "STORAGE_BACKEND")
        assert entry.get("sync") is False, f"{service['name']} pins STORAGE_BACKEND to {entry.get('value')!r}"
        assert "value" not in entry


def test_heroku_gives_the_s3_region_a_real_default(app_json: dict[str, Any]) -> None:
    """An empty config var is not an absent one.

    environ.Env returns its default only when the variable is unset, so a prompt
    left blank would reach boto3 as region_name="" rather than as the documented
    "auto". config/settings/base.py now coerces the blank back; this keeps the
    blueprint from creating it in the first place.
    """
    assert app_json["env"]["S3_REGION_NAME"]["value"] == "auto"


# ---------------------------------------------------------------------------
# railway.json
# ---------------------------------------------------------------------------


def test_railway_builds_the_dockerfile_for_both_services() -> None:
    for path in (RAILWAY_WEB, RAILWAY_WORKER):
        build = _load_json(path)["build"]
        assert build["builder"] == "DOCKERFILE"
        assert build["dockerfilePath"] == "Dockerfile"


def test_the_railway_web_service_migrates_and_is_health_checked() -> None:
    deploy = _load_json(RAILWAY_WEB)["deploy"]
    assert deploy["healthcheckPath"] == "/healthz"
    assert any("migrate" in command for command in deploy["preDeployCommand"])


def test_the_railway_worker_runs_the_queue_and_is_not_health_checked() -> None:
    """The worker serves no port, so a health check would fail it forever."""
    deploy = _load_json(RAILWAY_WORKER)["deploy"]
    assert deploy["startCommand"] == "python manage.py process_tasks"
    assert "healthcheckPath" not in deploy


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
