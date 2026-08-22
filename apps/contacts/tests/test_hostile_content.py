"""What the CRM renders when a stranger chose the words (SECURITY-BASELINE §2).

The contact list and the contact detail page are an attacker-content →
team-browser path: names, emails, platform usernames, avatar URLs and message
text all arrive from whoever messaged the workspace, and a CSV import lets an
operator paste in a file somebody else wrote.

Nothing in this app calls ``mark_safe``. These tests assert the consequence
rather than the rule, because the rule is only worth having if the pages behave.
"""

import pytest

from apps.contacts import activity, services
from apps.contacts.models import CustomFieldType
from apps.messaging.models import ContactChannelIdentity

SCRIPT = "<script>alert('xss')</script>"
BREAKOUT = '"><img src=x onerror=alert(1)>'


def url(tenancy, suffix: str) -> str:
    return f"/w/{tenancy.workspace.id}/{suffix}"


def identity(contact, **extra):
    row = ContactChannelIdentity(
        contact=contact, platform="telegram", platform_user_id=extra.pop("address", "12345"), extra=extra
    )
    row.save()
    return row


@pytest.mark.django_db
class TestEscaping:
    def test_a_script_tag_in_a_name_is_escaped_on_the_list(self, tenancy, client_for):
        services.create_contact(tenancy.workspace, first_name=SCRIPT)

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/")).content.decode()

        assert SCRIPT not in body
        assert "&lt;script&gt;" in body

    def test_a_script_tag_in_a_name_is_escaped_on_the_detail_page(self, tenancy, client_for):
        contact = services.create_contact(tenancy.workspace, first_name=SCRIPT)

        body = client_for(tenancy.owner).get(url(tenancy, f"contacts/{contact.pk}/")).content.decode()

        assert SCRIPT not in body

    def test_an_attribute_breakout_in_a_field_value_cannot_escape_its_input(self, tenancy, client_for):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        field = services.create_custom_field(tenancy.workspace, name="Note", field_type=CustomFieldType.TEXT)
        services.set_field_value(contact, field, BREAKOUT)

        body = client_for(tenancy.owner).get(url(tenancy, f"contacts/{contact.pk}/")).content.decode()

        # The payload survives as *text* — that is the point. What must not
        # survive is its structure: an unescaped quote to close the attribute and
        # an unescaped `<` to open a tag.
        assert BREAKOUT not in body
        assert "&quot;&gt;&lt;img" in body

    def test_html_in_a_tag_name_is_escaped_in_the_chips(self, tenancy, client_for):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        tag, _ = services.get_or_create_tag(tenancy.workspace, SCRIPT)
        services.add_tag(contact, tag)

        body = client_for(tenancy.owner).get(url(tenancy, f"contacts/{contact.pk}/")).content.decode()

        assert SCRIPT not in body

    def test_a_platform_username_is_escaped_in_the_channels_pane(self, tenancy, client_for):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        identity(contact, username=SCRIPT)

        body = client_for(tenancy.owner).get(url(tenancy, f"contacts/{contact.pk}/")).content.decode()

        assert SCRIPT not in body

    def test_a_hostile_tag_name_in_the_typeahead_cannot_break_the_hx_vals_json(self, tenancy, client_for):
        """The "create" row carries the search term inside a JSON attribute.
        escapejs is what keeps a quote a character rather than a delimiter."""
        contact = services.create_contact(tenancy.workspace, first_name="Ada")

        body = (
            client_for(tenancy.owner)
            .get(url(tenancy, f"contacts/{contact.pk}/tags/suggest/"), {"q": BREAKOUT})
            .content.decode()
        )

        assert BREAKOUT not in body
        # escapejs renders the quote as \u0022 inside the JSON attribute, so it
        # is a character in a string rather than the end of the attribute.
        assert "\\u0022" in body


@pytest.mark.django_db
class TestAvatarUrls:
    """``extra.profile_pic_url`` is whatever a platform sent. It becomes a
    ``src``, so it is scheme-checked before it ever reaches the template."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "vbscript:msgbox(1)",
            "//evil.test/x.png",
            "not a url at all",
            "",
        ],
    )
    def test_a_non_http_url_never_becomes_a_src(self, tenancy, hostile):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        identity(contact, profile_pic_url=hostile)

        assert activity.avatar_url(activity.identities_for(contact)) == ""

    def test_an_ordinary_https_url_is_kept(self, tenancy):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        identity(contact, profile_pic_url="https://cdn.example.test/a.png")

        assert activity.avatar_url(activity.identities_for(contact)) == "https://cdn.example.test/a.png"

    def test_a_non_string_profile_url_is_ignored_rather_than_stringified(self, tenancy):
        """``extra`` is jsonb, so this legitimately arrives as a number or a
        list, and ``str()`` on one would produce a src of ``['x']``."""
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        identity(contact, profile_pic_url=["https://evil.test/x.png"])

        assert activity.avatar_url(activity.identities_for(contact)) == ""

    def test_a_hostile_url_does_not_reach_the_rendered_page(self, tenancy, client_for):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        identity(contact, profile_pic_url="javascript:alert(1)")

        body = client_for(tenancy.owner).get(url(tenancy, f"contacts/{contact.pk}/")).content.decode()

        assert "javascript:" not in body


@pytest.mark.django_db
class TestMalformedPlatformData:
    def test_a_username_that_is_not_a_string_does_not_render_its_repr(self, tenancy):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        identity(contact, username={"nested": "object"})

        channel = activity.identities_for(contact)[0]

        assert isinstance(channel.username, str)

    def test_an_extra_that_is_not_a_dict_does_not_crash_the_pane(self, tenancy, client_for):
        contact = services.create_contact(tenancy.workspace, first_name="Ada")
        row = ContactChannelIdentity(contact=contact, platform="telegram", platform_user_id="1")
        row.save()
        ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(pk=row.pk).update(
            extra=["not", "a", "dict"]
        )

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/{contact.pk}/"))

        assert response.status_code == 200
