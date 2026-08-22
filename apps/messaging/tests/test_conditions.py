"""The ``window`` condition source (contract 8, SPEC §11.4)."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.common.platforms import Platform
from apps.contacts import conditions
from apps.contacts.services import create_contact
from apps.messaging.ingest import persist_events
from apps.messaging.models import ContactChannelIdentity
from apps.messaging.tests.conftest import make_connection, make_event

pytestmark = pytest.mark.django_db


def window_filter(platform: str, op: str) -> dict[str, Any]:
    return {"match": "all", "rules": [{"source": "window", "key": platform, "op": op}]}


def matching(workspace: Any, platform: str, op: str) -> set[Any]:
    return set(conditions.queryset(workspace, window_filter(platform, op)).values_list("pk", flat=True))


class TestRegistration:
    def test_the_slot_is_filled_after_app_loading(self) -> None:
        """The declaration ships with ``build_q=None`` and raises
        SourceNotEvaluableError until this issue lands. It has landed."""
        assert conditions.sources()["window"].is_evaluable

    def test_registering_twice_is_not_an_error(self) -> None:
        """``ready()`` runs twice under some autoreload paths. The registration
        is idempotent by dataclass equality, which is why ``build_q`` has to be
        a module-level function rather than a closure."""
        from apps.messaging.conditions import register_window_source

        register_window_source()
        register_window_source()

    def test_it_did_not_change_the_frozen_vocabulary(self) -> None:
        """Issue #6 embedded CONDITION_SCHEMA and the builder generates its
        panels from it: later layers supply behaviour, never vocabulary."""
        source = conditions.sources()["window"]
        assert source.ops == ("inside", "outside")
        assert source.key_kind == conditions.KEY_PLATFORM


class TestWindowedPlatforms:
    @pytest.fixture
    def tenancy_with_instagram(self, tenancy: Any) -> Any:
        return make_connection(tenancy.workspace, platform=Platform.INSTAGRAM, suffix="ig")

    def test_an_open_window_is_inside(self, tenancy: Any, tenancy_with_instagram: Any) -> None:
        persist_events(tenancy_with_instagram, [make_event(tenancy_with_instagram)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert matching(tenancy.workspace, Platform.INSTAGRAM, "inside") == {identity.contact_id}
        assert matching(tenancy.workspace, Platform.INSTAGRAM, "outside") == set()

    def test_an_expired_window_is_outside(self, tenancy: Any, tenancy_with_instagram: Any) -> None:
        persist_events(tenancy_with_instagram, [make_event(tenancy_with_instagram)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        ContactChannelIdentity.all_objects.filter(pk=identity.pk).update(
            window_expires_at=timezone.now() - timedelta(minutes=1)
        )
        assert matching(tenancy.workspace, Platform.INSTAGRAM, "inside") == set()
        assert matching(tenancy.workspace, Platform.INSTAGRAM, "outside") == {identity.contact_id}

    def test_an_opted_out_identity_is_outside_however_fresh_the_window(
        self, tenancy: Any, tenancy_with_instagram: Any
    ) -> None:
        """The only real use of this source is targeting a send. A filter that
        looks safe while quietly including people who said stop is worse than no
        filter (SPEC §19)."""
        persist_events(tenancy_with_instagram, [make_event(tenancy_with_instagram)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        ContactChannelIdentity.all_objects.filter(pk=identity.pk).update(opted_out_at=timezone.now())
        assert matching(tenancy.workspace, Platform.INSTAGRAM, "inside") == set()
        assert matching(tenancy.workspace, Platform.INSTAGRAM, "outside") == {identity.contact_id}


class TestWindowlessPlatforms:
    def test_reachable_means_inside_when_the_platform_has_no_window(self, tenancy: Any) -> None:
        """Telegram, SMS and email have window_hours=None, so their identities
        never carry a window_expires_at. A bare date comparison would put
        *nobody* inside, which is why has_window() is consulted first."""
        connection = make_connection(tenancy.workspace, platform=Platform.TELEGRAM, suffix="tg")
        persist_events(connection, [make_event(connection)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.window_expires_at is None
        assert matching(tenancy.workspace, Platform.TELEGRAM, "inside") == {identity.contact_id}


class TestAbsenceRule:
    def test_outside_matches_a_contact_with_no_identity_at_all(self, tenancy: Any, connection: Any) -> None:
        """NOT EXISTS, so a contact we have never heard from on this platform is
        outside — which is what makes the pair partition the workspace."""
        stranger = create_contact(tenancy.workspace, first_name="Never messaged")
        assert stranger.pk in matching(tenancy.workspace, Platform.TELEGRAM, "outside")

    def test_inside_and_outside_partition_the_workspace(self, tenancy: Any, connection: Any) -> None:
        create_contact(tenancy.workspace, first_name="Stranger")
        persist_events(connection, [make_event(connection)])
        inside = matching(tenancy.workspace, Platform.TELEGRAM, "inside")
        outside = matching(tenancy.workspace, Platform.TELEGRAM, "outside")
        everyone = matching(tenancy.workspace, Platform.TELEGRAM, "inside") | outside
        assert inside & outside == set()
        assert len(everyone) == 2

    def test_another_platforms_window_does_not_count(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection)])
        assert matching(tenancy.workspace, Platform.WHATSAPP, "inside") == set()


class TestTenancy:
    def test_the_subquery_is_scoped_to_the_workspace(self, tenancy: Any, other_tenancy: Any) -> None:
        """The scoping guard does not fire inside Exists(), so for_workspace() in
        the subquery is the only tenancy check there is. Prove it is in the SQL."""
        compiled = str(conditions.queryset(tenancy.workspace, window_filter(Platform.TELEGRAM, "inside")).query)
        assert compiled.count("workspace_id") >= 2

    def test_another_tenants_identity_never_matches(self, tenancy: Any, other_tenancy: Any) -> None:
        rival = make_connection(other_tenancy.workspace, platform=Platform.TELEGRAM, suffix="rival")
        persist_events(rival, [make_event(rival)])
        assert matching(tenancy.workspace, Platform.TELEGRAM, "inside") == set()
