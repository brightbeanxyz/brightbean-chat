"""Forms for the organization credential store.

The admin form override is not cosmetic — see :class:`PlatformCredentialAdminForm`.
"""

from typing import Any

from django import forms
from django.utils.safestring import mark_safe

from apps.credentials.models import (
    CONFIGURABLE_PLATFORMS,
    REQUIRED_CREDENTIAL_KEYS,
    PlatformCredential,
    derive_is_configured,
)

_KEY_HINTS = {
    platform: ", ".join(" / ".join(group) for group in groups) for platform, groups in REQUIRED_CREDENTIAL_KEYS.items()
}

CREDENTIALS_HELP = mark_safe(  # noqa: S308 - built from module constants, no user input
    'JSON object of app credentials, e.g. <code>{"client_id": "…", "client_secret": "…"}</code>. '
    "Required keys per platform:<br>" + "<br>".join(f"<b>{p}</b>: {hint}" for p, hint in _KEY_HINTS.items())
)


class CredentialsJSONFormMixin(forms.ModelForm):
    """Parse and render ``credentials`` as real JSON.

    ``EncryptedJSONField`` subclasses ``TextField``, so Django's ModelForm
    machinery generates a plain ``CharField`` for it. That is silently
    destructive: loading the page decrypts to a ``dict`` and renders it with
    ``str()`` — a Python repr with single quotes, which is not JSON — and saving
    with **no edits at all** cleans that back to a ``str``, which
    ``get_prep_value`` then ``json.dumps``es into a JSON *string literal*. The
    next read returns a ``str`` where every consumer expects a mapping:
    ``derive_is_configured`` raises ``AttributeError``, ``mask_credentials``
    raises, and the row stops working. Opening a credential page and pressing
    Save is enough to do it.

    ``forms.JSONField`` renders with ``json.dumps`` and cleans with
    ``json.loads``, so both ends of the round trip stay a mapping.
    """

    credentials = forms.JSONField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 8, "cols": 60}),
        help_text=CREDENTIALS_HELP,
    )

    # No Meta here on purpose: this class is never instantiated, and giving it a
    # model would make the mixin a second, concrete form.

    def clean_credentials(self) -> dict[str, str]:
        data = self.cleaned_data.get("credentials")
        if data in (None, ""):
            return {}
        if not isinstance(data, dict):
            raise forms.ValidationError(
                'Credentials must be a JSON object, e.g. {"client_id": "…", "client_secret": "…"}.'
            )
        cleaned: dict[str, str] = {}
        for key, value in data.items():
            if value is None:
                continue
            text = value if isinstance(value, str) else str(value)
            if text.strip():
                # Coerced to str so a numeric client_id typed as 12345 reaches
                # providers in the same shape as every other credential.
                cleaned[key] = text
        return cleaned

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        platform = cleaned.get("platform")
        credentials = cleaned.get("credentials") or {}
        if platform and credentials and not derive_is_configured(platform, credentials):
            hint = _KEY_HINTS.get(platform, "none")
            self.add_error(
                "credentials",
                f"Missing required keys for {platform}. Expected: {hint}. "
                "The row stays inactive until every required key is present.",
            )
        return cleaned


class PlatformCredentialAdminForm(CredentialsJSONFormMixin):
    class Meta:
        model = PlatformCredential
        fields = ("organization", "platform", "credentials")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Only platforms that have deployment-level app credentials at all.
        platform_field: Any = self.fields["platform"]
        platform_field.choices = [
            choice for choice in platform_field.choices if choice[0] == "" or choice[0] in CONFIGURABLE_PLATFORMS
        ]
