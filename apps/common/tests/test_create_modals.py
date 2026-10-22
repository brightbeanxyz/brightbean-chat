"""Every create form in the product asks for its name in a modal.

They used to be one of two things: a bare field with a "Create" button stuck to
its side in the page header, or a header button that unfolded a card holding the
same field and button. Both are now a header button that opens a
`{% modal %}` (apps/common/templatetags/common_extras.py), so this walks every
page that had one and checks the dialog is there and the inline form is not.

The tag itself is tested in test_templatetags.py; the sequence page, which was
the one that prompted this, has its own tests in apps/campaigns.
"""

import re
from typing import Any

import pytest
from django.urls import reverse

from apps.channels.models import ChannelConnection, ConnectionStatus, Platform

#: (url name, needs the workspace id, dialog id, the event that closes it — or
#: "" for a form that navigates away instead).
PAGES = [
    ("flows:list", True, "new-flow", "flowsChanged"),
    ("broadcasts:list", True, "new-broadcast", ""),
    ("contacts:tag_list", True, "new-tag", "tagsChanged"),
    ("contacts:field_list", True, "new-field", "fieldsChanged"),
    ("contacts:list", True, "save-segment", "segmentsChanged"),
    ("inbox:label_settings", True, "new-label", "inboxLabelsChanged"),
    ("media:library", True, "new-folder", "mediaChanged"),
    ("api_webhooks:list", True, "add-endpoint", ""),
    ("organizations:workspaces", False, "new-workspace", ""),
    ("settings_org_api_keys", False, "issue-key", ""),
    ("members:list", False, "invite-member", ""),
]

#: The flag each page's inline disclosure used to hang on.
OLD_FLAGS = re.compile(r'x-show="(adding|creating|inviting)"')


def _page(tenancy: Any, url_name: str, scoped: bool) -> str:
    return reverse(url_name, kwargs={"workspace_id": tenancy.workspace.id} if scoped else None)


@pytest.mark.django_db
class TestEveryCreateFormIsAModal:
    @pytest.fixture(autouse=True)
    def _a_broadcastable_channel(self, tenancy: Any) -> None:
        """The broadcast page offers creation only when a channel can carry one."""
        ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="Test bot",
            external_id=f"bot-{tenancy.slug}",
            status=ConnectionStatus.ACTIVE,
        )

    @pytest.mark.parametrize(("url_name", "scoped", "dialog_id", "close_on"), PAGES)
    def test_the_button_opens_a_dialog_holding_the_form(
        self, tenancy, client_for, url_name, scoped, dialog_id, close_on
    ):
        response = client_for(tenancy.owner).get(_page(tenancy, url_name, scoped))
        assert response.status_code == 200
        body = response.content.decode()

        assert f"$modal('{dialog_id}')" in body
        start = body.index(f'<dialog id="{dialog_id}"')
        dialog = body[start : body.index("</dialog>", start)]
        assert f"x-data=\"bbModal('{close_on}')\"" in dialog
        assert "<form" in dialog
        assert 'type="submit"' in dialog
        assert "autofocus" in dialog, "showModal() focuses the [autofocus] field — the old $refs.focus() is gone"

    @pytest.mark.parametrize(("url_name", "scoped", "dialog_id", "close_on"), PAGES)
    def test_the_inline_disclosure_is_gone(self, tenancy, client_for, url_name, scoped, dialog_id, close_on):
        body = client_for(tenancy.owner).get(_page(tenancy, url_name, scoped)).content.decode()

        assert not OLD_FLAGS.search(body)


@pytest.mark.django_db
class TestAPageThatAnswersARefusalByReRenderingOpensItsDialog:
    """API keys and webhooks answer a refused POST by re-rendering the list at
    400 rather than redirecting. A dialog that stayed shut would hide the form
    that produced the message, so it opens itself, and the message is inside it."""

    def test_issuing_a_key_without_a_workspace(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(reverse("api_keys_issue"), {"name": "Zapier"})

        assert response.status_code == 400
        body = response.content.decode()
        start = body.index('<dialog id="issue-key"')
        dialog = body[start : body.index("</dialog>", start)]
        assert 'x-init="$nextTick(() => show())"' in dialog
        assert '<div class="alert-error mb-5">Choose a workspace.</div>' in dialog
        assert 'value="Zapier"' in dialog

    def test_adding_a_webhook_with_a_bad_url(self, tenancy, client_for):
        url = reverse("api_webhooks:create", kwargs={"workspace_id": tenancy.workspace.id})
        response = client_for(tenancy.owner).post(url, {"url": "not a url", "events": ["message.received"]})

        assert response.status_code == 400
        body = response.content.decode()
        start = body.index('<dialog id="add-endpoint"')
        dialog = body[start : body.index("</dialog>", start)]
        assert 'x-init="$nextTick(() => show())"' in dialog
        assert "alert-error" in dialog

    def test_a_plain_visit_leaves_it_shut(self, tenancy, client_for):
        body = client_for(tenancy.owner).get(reverse("settings_org_api_keys")).content.decode()

        assert "show()" not in body


@pytest.mark.django_db
class TestCancelInsideANestedScopeClosesTheDialog:
    """The flows dialog's form has an x-data of its own (the "Start from"
    picker), and its Cancel calls the dialog's close(). bbModal used to find its
    dialog through `this.$root`, which from inside that form is the form — so
    Cancel threw "this.$root.close is not a function" and the dialog stayed
    open. It now reads `$root` once, in init(), where it is the dialog."""

    def test_the_flows_dialog_nests_a_scope_around_its_cancel(self, tenancy, client_for):
        url = reverse("flows:list", kwargs={"workspace_id": tenancy.workspace.id})
        body = client_for(tenancy.owner).get(url).content.decode()

        start = body.index('<dialog id="new-flow"')
        dialog = body[start : body.index("</dialog>", start)]
        form = dialog[dialog.index("<form") :]
        assert "x-data=" in form[: form.index(">")]
        assert '@click="close()">Cancel</button>' in form

    def test_the_behaviour_reads_root_only_in_init(self, tenancy, client_for):
        url = reverse("flows:list", kwargs={"workspace_id": tenancy.workspace.id})
        body = client_for(tenancy.owner).get(url).content.decode()

        start = body.index("window.bbModal = function")
        script = body[start : body.index("</script>", start)]
        assert script.count("$root") == 1
        assert "init: function () {\n          dialog = this.$root;" in script
