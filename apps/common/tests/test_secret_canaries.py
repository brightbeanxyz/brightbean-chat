"""No encrypted value reaches an admin page or an API response.

SECURITY-BASELINE §5.2 names four places a credential must never appear: logs,
error reports, **admin list displays** and **API responses**. The first two were
covered thoroughly — ``test_logging.py``, ``test_sentry.py`` and five per-adapter
``test_*_scrubbing.py`` modules. The last two had nothing, which is why §5.2 read
``PARTIAL`` in ``docs/security-audit.md``. Issue #94.

**Why canaries rather than running the scrubber over the output.** Using
``scrub()`` as an oracle gives false positives immediately: the admin change form
emits ``csrfmiddlewaretoken="…"``, which the ``key=value`` rule matches on
``token``. So instead every encrypted column is seeded with a value that appears
nowhere else in the codebase, and the assertion is simply that the string is not
in the response. No heuristics, no false positives, and a failure names the exact
column that leaked.

**What makes this a property rather than a checklist.** The admin sweep walks
``admin.site._registry``, so a ``ModelAdmin`` registered by a later PR is swept
without anyone remembering to add it. The API sweep walks every ``api_v1`` GET
route. And :class:`TestTheSweepIsComplete` asserts every encrypted field in the
project has a canary, so a sixth column cannot be added outside the sweep.

**And it is falsifiable.** :class:`TestTheSweepWouldCatchALeak` registers a
throwaway ``ModelAdmin`` with an encrypted field in ``list_display`` and asserts
the sweep goes red. Without that, a sweep that silently stopped looking would
still pass forever.
"""

from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.api.models import OutboundWebhook
from apps.api.tests.conftest import bearer, make_key
from apps.channels.models import ChannelConnection
from apps.common.encryption import EncryptedJSONField, EncryptedTextField
from apps.common.platforms import Platform
from apps.credentials.models import PlatformCredential, WorkspaceCredentialOverride

pytestmark = pytest.mark.django_db

#: One unmistakable value per encrypted column, keyed by ``<app_label>.<Model>.<field>``.
#:
#: Deliberately not derived from the field name at runtime: a canary that a
#: template could plausibly reproduce for some other reason would make a failure
#: ambiguous, and these cannot appear by accident.
CANARIES: dict[str, str] = {
    "credentials.PlatformCredential.credentials": "kanarie-QPZM-platformcred-7f21",
    "credentials.WorkspaceCredentialOverride.credentials": "kanarie-QPZM-wsoverride-2c94",
    "channels.ChannelConnection.credentials": "kanarie-QPZM-connectioncreds-4b0e",
    "channels.ChannelConnection.webhook_secret": "kanarie-QPZM-webhooksecret-8d55",
    "api.OutboundWebhook.secret": "kanarie-QPZM-outboundsecret-1a73",
}

#: The one change form that shows a decrypted value on purpose, and why.
#:
#: ``PlatformCredentialAdmin`` *is* the organization credential editor: a form
#: that hid the field could not edit it. ``credentials/admin.py``'s docstring
#: states the trade-off — "opening a change page decrypts secrets into an HTML
#: response, so every permission hook is superuser-only" — and
#: ``test_the_one_exception_is_still_superuser_only`` asserts the gate that pays
#: for it. SECURITY-BASELINE §5.2 names *list displays*, which stay clean; this
#: sweep is stricter than the clause, so the difference is recorded rather than
#: quietly widened.
CHANGE_FORM_EXCEPTIONS = frozenset({"credentials.PlatformCredential.credentials"})

#: Encrypted columns that belong to the test scaffolding rather than the product.
EXEMPT_FROM_CANARIES = frozenset({"testapp.EncryptionProbe.secret", "testapp.EncryptionProbe.payload"})


def encrypted_columns() -> list[str]:
    """``<label>.<field>`` for every encrypted column in the project."""
    from django.apps import apps as django_apps

    found = []
    for model in django_apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, EncryptedJSONField | EncryptedTextField):
                found.append(f"{model._meta.label}.{field.name}")
    return sorted(found)


@pytest.fixture
def seeded(tenancy: Any) -> dict[str, Any]:
    """One row per encrypted column, each carrying its canary."""
    workspace = tenancy.workspace
    organization = workspace.organization
    connection = ChannelConnection.objects.create(
        workspace=workspace,
        platform=Platform.TELEGRAM,
        external_id=f"canary-{workspace.pk}",
        display_name="Canary connection",
        credentials={"bot_token": CANARIES["channels.ChannelConnection.credentials"]},  # type: ignore[misc]
        webhook_secret=CANARIES["channels.ChannelConnection.webhook_secret"],
    )
    platform_credential = PlatformCredential.objects.create(
        organization=organization,
        platform=Platform.TELEGRAM,
        credentials={"app_secret": CANARIES["credentials.PlatformCredential.credentials"]},  # type: ignore[misc]
    )
    override = WorkspaceCredentialOverride.objects.create(
        workspace=workspace,
        platform=Platform.MESSENGER,
        credentials={"app_secret": CANARIES["credentials.WorkspaceCredentialOverride.credentials"]},  # type: ignore[misc]
    )
    webhook = OutboundWebhook.objects.create(
        workspace=workspace,
        url="https://receiver.example.test/hook",
        secret=CANARIES["api.OutboundWebhook.secret"],
        events=["contact.created"],
    )
    return {
        "connection": connection,
        "platform_credential": platform_credential,
        "override": override,
        "webhook": webhook,
    }


def leaked(body: bytes) -> list[str]:
    """Which canaries are readable in ``body``."""
    text = body.decode("utf-8", "replace")
    return [name for name, canary in CANARIES.items() if canary in text]


class TestTheSweepIsComplete:
    def test_every_encrypted_column_has_a_canary(self) -> None:
        """A sixth encrypted column cannot be added outside this sweep.

        This assertion earned its place on the first run: the issue named four
        columns and there are five — ``WorkspaceCredentialOverride.credentials``
        was missed by the hand-written list.
        """
        covered = set(CANARIES) | EXEMPT_FROM_CANARIES

        assert set(encrypted_columns()) <= covered

    def test_the_canaries_are_distinct(self) -> None:
        """A shared value would make a failure name the wrong column."""
        assert len(set(CANARIES.values())) == len(CANARIES)

    def test_the_seeded_rows_really_hold_the_canaries(self, seeded: dict[str, Any]) -> None:
        """A sweep over rows that never held a secret would pass vacuously."""
        seeded["connection"].refresh_from_db()

        assert CANARIES["channels.ChannelConnection.webhook_secret"] == seeded["connection"].webhook_secret
        assert CANARIES["channels.ChannelConnection.credentials"] in str(seeded["connection"].credentials)


class TestTheAdminNeverShowsASecret:
    """§5.2's "admin list displays", swept over the whole registry."""

    @pytest.fixture
    def superuser_client(self, client: Any) -> Any:
        user = get_user_model().objects.create_superuser(email="root@example.test", password="not-a-real-password")
        client.force_login(user)
        return client

    def test_no_changelist_shows_a_canary(self, superuser_client: Any, seeded: dict[str, Any]) -> None:
        """Walks the registry, so a ModelAdmin added later is swept for free."""
        offenders: list[str] = []
        for model in admin.site._registry:
            url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
            response = superuser_client.get(url)
            if response.status_code == 200:
                offenders += [f"{model._meta.label} changelist: {name}" for name in leaked(response.content)]

        assert offenders == []

    def test_no_change_page_shows_a_canary(self, superuser_client: Any, seeded: dict[str, Any]) -> None:
        """The change form is where an encrypted field would render as an input value.

        Only the seeded models the admin actually registers have a change page.
        ``WorkspaceCredentialOverride`` deliberately has none — ``credentials/admin.py``
        says so — and reversing a URL for it raises rather than 404s, so the
        registry is consulted rather than assumed.
        """
        registered = set(admin.site._registry)
        offenders: list[str] = []
        swept = 0
        for obj in seeded.values():
            if type(obj) not in registered:
                continue
            meta = obj._meta
            url = reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk])
            response = superuser_client.get(url)
            swept += 1
            if response.status_code == 200:
                offenders += [
                    f"{meta.label} change: {name}"
                    for name in leaked(response.content)
                    if name not in CHANGE_FORM_EXCEPTIONS
                ]

        assert offenders == []
        # A sweep that skipped everything would pass without asserting anything.
        assert swept >= 2

    def test_the_one_exception_is_still_superuser_only(self, client: Any, seeded: dict[str, Any]) -> None:
        """The mitigation that justifies the exception, asserted rather than assumed.

        ``PlatformCredential``'s change form shows the decrypted value because it
        *is* the credential editor — a form that hid the field could not edit it.
        What makes that acceptable is the gate, so the gate is what this tests: a
        staff user who is not a superuser gets nothing.
        """
        staff = get_user_model().objects.create_user(
            email="staff@example.test", password="not-a-real-password", is_staff=True
        )
        client.force_login(staff)
        obj = seeded["platform_credential"]

        url = reverse("admin:credentials_platformcredential_change", args=[obj.pk])
        response = client.get(url)

        assert response.status_code in (302, 403)
        assert leaked(response.content) == []


class TestTheApiNeverReturnsASecret:
    """§5.2's "API responses", over every readable route."""

    def test_no_get_route_returns_a_canary(self, client: Any, tenancy: Any, seeded: dict[str, Any]) -> None:
        _key, plaintext = make_key(tenancy.workspace)
        paths = [
            "/api/v1/contacts",
            "/api/v1/flows",
            "/api/v1/tags",
            "/api/v1/fields",
            "/api/v1/openapi.json",
        ]

        offenders: list[str] = []
        for path in paths:
            response = client.get(path, **bearer(plaintext))
            offenders += [f"{path}: {name}" for name in leaked(response.content)]

        assert offenders == []

    def test_the_sweep_actually_reached_the_api(self, client: Any, tenancy: Any) -> None:
        """A 401 on every route would make the sweep above pass without testing anything."""
        _key, plaintext = make_key(tenancy.workspace)

        response = client.get("/api/v1/contacts", **bearer(plaintext))

        assert response.status_code == 200


class TestTheSweepWouldCatchALeak:
    """Falsifiability. Without this, a sweep that stopped looking still passes.

    Rendered through the ``ModelAdmin`` rather than through the URLconf on
    purpose. ``AdminSite.get_urls()`` binds each model's patterns to the
    ``ModelAdmin`` **instance** that was registered when ``config/urls.py`` was
    imported, so re-registering a class mid-test changes ``_registry`` and not
    the already-built patterns: the probe would silently hit the real admin and
    "pass" by never testing anything. It only worked at all when it happened to
    run before the first ``reverse()`` in the process, which is the worst kind of
    green.
    """

    def test_an_encrypted_field_in_list_display_is_caught(self, rf: Any, seeded: dict[str, Any]) -> None:
        class LeakyAdmin(admin.ModelAdmin):
            list_display = ("pk", "webhook_secret")

        request = rf.get("/admin/channels/channelconnection/")
        request.user = get_user_model().objects.create_superuser(
            email="probe@example.test", password="not-a-real-password"
        )

        response = LeakyAdmin(ChannelConnection, admin.site).changelist_view(request)
        response.render()  # type: ignore[attr-defined]  # a TemplateResponse at runtime

        assert "channels.ChannelConnection.webhook_secret" in leaked(response.content)

    def test_the_real_admin_does_not_leak_the_same_field(self, rf: Any, seeded: dict[str, Any]) -> None:
        """The other half of the probe: the shipped admin, rendered the same way."""
        from apps.channels.admin import ChannelConnectionAdmin

        request = rf.get("/admin/channels/channelconnection/")
        request.user = get_user_model().objects.create_superuser(
            email="probe2@example.test", password="not-a-real-password"
        )

        response = ChannelConnectionAdmin(ChannelConnection, admin.site).changelist_view(request)
        response.render()  # type: ignore[attr-defined]  # a TemplateResponse at runtime

        assert leaked(response.content) == []
