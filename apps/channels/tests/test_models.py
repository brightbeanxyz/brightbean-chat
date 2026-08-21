"""ChannelConnection and WebhookEventLog (SPEC §5)."""

from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.channels.models import ChannelConnection, WebhookEventLog, generate_webhook_secret
from apps.common.platforms import Platform
from apps.common.scoping import UnscopedQueryError

pytestmark = pytest.mark.django_db


def make_connection(workspace: Any, **overrides: Any) -> ChannelConnection:
    fields = {
        "platform": Platform.TELEGRAM,
        "display_name": "Bot",
        "external_id": f"ext-{workspace.pk}",
        **overrides,
    }
    connection = ChannelConnection(workspace=workspace, **fields)
    connection.rotate_webhook_secret()
    connection.save()
    return connection


class TestTenancy:
    def test_the_manager_refuses_an_unscoped_query(self, tenancy: Any) -> None:
        make_connection(tenancy.workspace)
        with pytest.raises(UnscopedQueryError):
            list(ChannelConnection.objects.all())

    def test_scoped_queries_see_only_their_workspace(self, tenancy: Any, other_tenancy: Any) -> None:
        mine = make_connection(tenancy.workspace)
        make_connection(other_tenancy.workspace)
        assert list(ChannelConnection.objects.for_workspace(tenancy.workspace)) == [mine]


class TestSecretStorage:
    def test_the_secret_is_encrypted_at_rest(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace)
        secret = connection.webhook_secret

        # Read the column without the field's decryption, the way a database
        # dump would (SECURITY-BASELINE §5).
        with transaction.atomic():
            from django.db import connection as db

            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT webhook_secret FROM channels_channel_connection WHERE id = %s",
                    [str(connection.pk)],
                )
                stored = cursor.fetchone()[0]
        assert secret not in stored
        assert stored != secret

    def test_the_digest_is_deterministic_and_queryable(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace)
        found = ChannelConnection.resolve_by_webhook_secret(connection.webhook_secret)
        assert found == connection

    def test_filtering_on_the_encrypted_column_finds_nothing(self, tenancy: Any) -> None:
        """The trap CONTRIBUTING warns about, asserted so nobody rediscovers it."""
        connection = make_connection(tenancy.workspace)
        assert (
            not ChannelConnection.objects.for_workspace(tenancy.workspace)
            .filter(webhook_secret=connection.webhook_secret)
            .exists()
        )

    def test_an_empty_secret_resolves_to_nothing(self, tenancy: Any) -> None:
        # Rows with no secret share an empty digest; presenting "" must not
        # match them.
        ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.SMS,
            display_name="No secret",
            external_id="+15550000",
        )
        assert ChannelConnection.resolve_by_webhook_secret("") is None

    def test_rotation_replaces_both_halves(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace)
        old_secret, old_digest = connection.webhook_secret, connection.webhook_secret_digest
        new_secret = connection.rotate_webhook_secret()
        connection.save()

        assert new_secret != old_secret
        assert connection.webhook_secret_digest != old_digest
        assert ChannelConnection.resolve_by_webhook_secret(old_secret) is None
        assert ChannelConnection.resolve_by_webhook_secret(new_secret) == connection

    def test_verify_is_exact(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace)
        assert connection.verify_webhook_secret(connection.webhook_secret) is True
        assert connection.verify_webhook_secret(connection.webhook_secret + "x") is False
        assert connection.verify_webhook_secret("") is False

    def test_generated_secrets_are_unique_and_long(self) -> None:
        secrets = {generate_webhook_secret() for _ in range(50)}
        assert len(secrets) == 50
        assert all(len(secret) >= 32 for secret in secrets)


class TestConstraints:
    def test_one_account_cannot_be_connected_twice(self, tenancy: Any, other_tenancy: Any) -> None:
        """SPEC §5's unique (platform, external_id) is deployment-wide."""
        make_connection(tenancy.workspace, external_id="shared-bot")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_connection(other_tenancy.workspace, external_id="shared-bot")

    def test_the_same_account_id_on_another_platform_is_fine(self, tenancy: Any) -> None:
        make_connection(tenancy.workspace, platform=Platform.TELEGRAM, external_id="12345")
        make_connection(tenancy.workspace, platform=Platform.MESSENGER, external_id="12345")

    def test_several_connections_may_have_no_secret(self, tenancy: Any) -> None:
        """The digest's unique constraint is partial; empty is not a value."""
        for index in range(3):
            ChannelConnection.objects.create(
                workspace=tenancy.workspace,
                platform=Platform.EMAIL,
                display_name=f"Domain {index}",
                external_id=f"mail-{index}.test",
            )
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 3


class TestWebhookEventLog:
    def test_an_event_is_logged_once_per_connection(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace)
        WebhookEventLog.objects.create(connection=connection, platform=connection.platform, provider_event_id="e1")
        with pytest.raises(IntegrityError), transaction.atomic():
            WebhookEventLog.objects.create(connection=connection, platform=connection.platform, provider_event_id="e1")

    def test_the_same_event_id_on_another_connection_is_a_different_event(self, tenancy: Any) -> None:
        first = make_connection(tenancy.workspace, external_id="a")
        second = make_connection(tenancy.workspace, platform=Platform.MESSENGER, external_id="b")
        WebhookEventLog.objects.create(connection=first, platform=first.platform, provider_event_id="e1")
        WebhookEventLog.objects.create(connection=second, platform=second.platform, provider_event_id="e1")
        assert WebhookEventLog.objects.count() == 2

    def test_deleting_a_connection_takes_its_log_with_it(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace)
        WebhookEventLog.objects.create(connection=connection, platform=connection.platform, provider_event_id="e1")
        connection.delete()
        assert WebhookEventLog.objects.count() == 0

    def test_mark_records_the_outcome(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace)
        row = WebhookEventLog.objects.create(
            connection=connection, platform=connection.platform, provider_event_id="e1"
        )
        row.mark("processed")
        row.refresh_from_db()
        assert row.status == "processed"
        assert row.processed_at is not None
