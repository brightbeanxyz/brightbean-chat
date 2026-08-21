"""Forms for connection management.

Deliberately small. Issue #4 ships the platform-agnostic frame — a connection
row, its status and its webhook secret — and every real connect flow (a
BotFather token, a Meta OAuth round trip, Twilio credentials) belongs to the
adapter issue for that platform. A form here that asked for credentials would be
guessing at six different shapes and would have to be replaced six times.
"""

from typing import Any

from django import forms

from apps.channels.models import ChannelConnection
from apps.common.platforms import Platform


class ChannelConnectionForm(forms.ModelForm):
    """Create a connection: which platform, what to call it, which account.

    ``credentials`` is absent on purpose (see the module docstring), and
    ``webhook_secret`` is never a form field — it is minted by the view and
    shown once.
    """

    class Meta:
        model = ChannelConnection
        fields = ["platform", "display_name", "external_id"]
        labels = {
            "display_name": "Name",
            "external_id": "Account identifier",
        }
        help_texts = {
            "display_name": "How this channel appears in the inbox and the flow builder.",
            "external_id": "The platform's own id for the account: bot id, page id, phone number id, or sending domain.",
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["platform"].choices = Platform.choices  # type: ignore[attr-defined]

    def clean_external_id(self) -> str:
        return (self.cleaned_data.get("external_id") or "").strip()

    def clean_display_name(self) -> str:
        return (self.cleaned_data.get("display_name") or "").strip()

    def clean(self) -> dict[str, Any]:
        """Reject a duplicate before the database does, with a careful message.

        SPEC §5's ``unique (platform, external_id)`` is **deployment-wide**, not
        per workspace: one Telegram bot cannot serve two workspaces, because the
        second would silently take over the first one's inbound traffic.

        The consequence is that this check can be told about a row in a
        workspace the caller cannot see, so the message says the account is
        connected *to this deployment* and never which workspace holds it. A
        message naming the workspace would be a cross-tenant disclosure
        (SECURITY-BASELINE §1); saying nothing at all would leave the operator
        staring at an IntegrityError.
        """
        # ModelForm.clean() is typed as returning an optional dict; in practice
        # it returns self.cleaned_data, and reading that directly keeps mypy and
        # the runtime agreeing.
        super().clean()
        cleaned: dict[str, Any] = self.cleaned_data
        platform = cleaned.get("platform")
        external_id = cleaned.get("external_id")
        if not platform or not external_id:
            return cleaned

        # Cross-tenant by necessity: the constraint being enforced is itself
        # deployment-wide, so a workspace-scoped check could not see the row
        # that will actually collide.
        clash = ChannelConnection.objects.unscoped().filter(platform=platform, external_id=external_id)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            self.add_error(
                "external_id",
                "That account is already connected to this deployment. Disconnect it there first.",
            )
        return cleaned
