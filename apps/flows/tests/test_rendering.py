"""The renderer, and SECURITY-BASELINE §3's SSTI ban as executable assertions.

The hostile half of this module is the point. Baseline §3 says user content is
"never evaluated by Django/Jinja template engines" and the way to keep that true
in a year is a suite that fails the moment somebody reaches for one — so the
tests below feed the renderer every template syntax a Python web app might
plausibly grow, from *inside a contact's own field values*, and assert the output
is the literal text.
"""

import itertools
import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.contacts.services import create_custom_field, set_field_value
from apps.flows.rendering import (
    MAX_RENDERED_CHARS,
    RenderContext,
    context_for,
    render,
    render_json,
)
from apps.flows.tests.support import contact_for

#: Every string a template engine, a shell or a browser might treat as code.
#: Fed in as *values*, which is where attacker-controlled text actually arrives.
HOSTILE_VALUES = (
    "{{ 7*7 }}",
    "{{first_name}}",
    "{%- for x in ().__class__.__base__.__subclasses__() -%}{{x}}{%- endfor -%}",
    "{% load i18n %}",
    "${jndi:ldap://evil.test/a}",
    "#{7*7}",
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "'; DROP TABLE contacts; --",
    "{{ settings.SECRET_KEY }}",
    "{{{{first_name}}}}",
    "\\u007b\\u007bfirst_name\\u007d\\u007d",
)


def ctx(**namespaces) -> RenderContext:
    """A context from literals — no database, so the grammar can be tested alone."""
    return RenderContext(
        system=namespaces.get("system", {}),
        custom_fields=namespaces.get("custom_fields", {}),
        variables=namespaces.get("variables", {}),
    )


class TestSubstitution:
    def test_a_token_is_replaced(self):
        assert render("Hi {{first_name}}!", ctx(system={"first_name": "Ada"})) == "Hi Ada!"

    @pytest.mark.parametrize("template", ["{{first_name}}", "{{ first_name }}", "{{   first_name   }}"])
    def test_inner_whitespace_is_ignored(self, template):
        assert render(template, ctx(system={"first_name": "Ada"})) == "Ada"

    def test_lookup_is_case_insensitive(self):
        assert render("{{First_Name}}", ctx(system={"first_name": "Ada"})) == "Ada"

    def test_an_unknown_token_renders_empty(self):
        """Empty, not literal: echoing the author's template at a contact is worse."""
        assert render("Hi {{nope}}!", ctx()) == "Hi !"

    def test_several_tokens_in_one_string(self):
        context = ctx(system={"first_name": "Ada", "last_name": "Lovelace"})
        assert render("{{first_name}} {{last_name}}", context) == "Ada Lovelace"

    @pytest.mark.parametrize("template", [None, 42, "", [], {}])
    def test_a_non_string_template_renders_empty(self, template):
        assert render(template, ctx()) == ""

    def test_an_unknown_mode_is_a_programming_error(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            render("x", ctx(), mode="markdown")


class TestPrecedence:
    """SPEC §9.2's order, fixed: system field, then custom field, then variable."""

    def test_system_field_beats_a_custom_field_of_the_same_name(self):
        context = ctx(system={"first_name": "Ada"}, custom_fields={"first_name": "Grace"})
        assert render("{{first_name}}", context) == "Ada"

    def test_custom_field_beats_a_variable_of_the_same_name(self):
        context = ctx(custom_fields={"stage": "trial"}, variables={"stage": "lead"})
        assert render("{{stage}}", context) == "trial"

    def test_a_variable_resolves_when_nothing_else_claims_the_name(self):
        assert render("{{code}}", ctx(variables={"code": "1234"})) == "1234"


class TestValueCoercion:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""),
            (True, "yes"),
            (False, "no"),
            (7, "7"),
            (Decimal("2.50"), "2.5"),
            (Decimal("10"), "10"),
            (date(2026, 8, 22), "2026-08-22"),
            (datetime(2026, 8, 22, 9, 30, 15, tzinfo=UTC), "2026-08-22T09:30+00:00"),
        ],
    )
    def test_values_render_the_way_a_person_would_write_them(self, value, expected):
        assert render("{{v}}", ctx(variables={"v": value})) == expected

    def test_output_is_capped(self):
        long_value = "x" * (MAX_RENDERED_CHARS * 2)
        assert len(render("{{v}}", ctx(variables={"v": long_value}))) <= MAX_RENDERED_CHARS


class TestSstiBan:
    """SECURITY-BASELINE §3. Hostile *values*, never evaluated, never rescanned."""

    @pytest.mark.parametrize("hostile", HOSTILE_VALUES)
    def test_a_hostile_value_renders_as_itself_in_text_mode(self, hostile):
        rendered = render("Hello {{first_name}}", ctx(system={"first_name": hostile}))
        assert rendered == f"Hello {hostile}"

    @pytest.mark.parametrize("hostile", HOSTILE_VALUES)
    def test_a_hostile_value_is_escaped_in_html_mode(self, hostile):
        rendered = render("<p>{{first_name}}</p>", ctx(system={"first_name": hostile}), mode="html")
        assert "<script" not in rendered
        assert "<img" not in rendered
        # The author's own markup is untouched; only the value is escaped.
        assert rendered.startswith("<p>") and rendered.endswith("</p>")

    def test_a_value_that_is_itself_a_placeholder_is_not_resolved(self):
        """The single-pass rule. A contact named ``{{email}}`` leaks nothing."""
        context = ctx(system={"first_name": "{{email}}", "email": "ada@example.test"})
        assert render("{{first_name}}", context) == "{{email}}"

    def test_nested_braces_do_not_compose_into_a_second_token(self):
        context = ctx(variables={"a": "{{", "b": "email}}", "email": "secret@example.test"})
        assert render("{{a}}{{b}}", context) == "{{email}}"

    def test_template_syntax_in_the_template_itself_is_left_alone(self):
        """Only ``{{token}}`` is a placeholder. Django tag syntax is plain text."""
        assert render("{% if x %}hi{% endif %}", ctx()) == "{% if x %}hi{% endif %}"

    @pytest.mark.parametrize(
        "expression",
        [
            "{{ ''.__class__.__mro__ }}",
            "{{ first_name|upper }}",
            "{{ first_name() }}",
            "{{ items[0] }}",
        ],
    )
    def test_expression_syntax_is_not_even_a_token(self, expression):
        """Quotes, pipes, parentheses and brackets are outside the grammar.

        Nothing that could express a filter, a call or an index is a token, so
        these are not substituted at all — they come back as the literal text an
        author typed, which is the honest answer for something that was never
        going to work.
        """
        context = ctx(system={"first_name": "Ada"})
        assert render(expression, context) == expression

    @pytest.mark.parametrize(
        "expression",
        ["{{ contact.__class__ }}", "{{ __class__ }}", "{{ x.y.z }}", "{{ a if b else c }}"],
    )
    def test_dotted_names_are_looked_up_and_miss(self, expression):
        """A dot is a legal *name* character (a field called "utm.source"), so a
        dotted expression is a token — and a token is a dictionary lookup.

        There is no attribute access to reach: ``contact.__class__`` is one
        string that no namespace has a key for, so it renders as nothing. That
        is the whole difference between substitution and evaluation, and it is
        worth an assertion of its own rather than being lumped in above.
        """
        context = ctx(system={"first_name": "Ada"})
        assert render(expression, context) == ""

    def test_the_renderer_never_touches_a_template_engine(self, monkeypatch):
        """A structural assertion: importing one would make this fail loudly."""
        import django.template

        def _boom(*args, **kwargs):
            raise AssertionError("The renderer must never construct a Django Template (baseline §3).")

        monkeypatch.setattr(django.template, "Template", _boom)
        monkeypatch.setattr(django.template.engines, "__getitem__", _boom)
        assert render("Hi {{first_name}}", ctx(system={"first_name": "{{ 7*7 }}"})) == "Hi {{ 7*7 }}"


class TestFuzz:
    """Baseline §3: "renderer fuzzed". Seeded, so a failure is reproducible."""

    ALPHABET = "{}%$#()[]|.\\'\"<>&; abcXY7_-"

    def test_random_hostile_input_never_produces_a_surviving_placeholder(self):
        rng = random.Random(20260822)  # noqa: S311 - test input generation, not a security decision
        for _ in range(3000):
            value = "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 40)))
            template = "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 40)))
            rendered = render(f"{template}{{{{v}}}}{template}", ctx(variables={"v": value}))
            # The substituted value appears verbatim: nothing in it was consumed,
            # re-scanned or evaluated, whatever it happened to contain.
            assert value in rendered

    def test_random_templates_never_raise(self):
        rng = random.Random(1789)  # noqa: S311 - test input generation
        context = ctx(system={"first_name": "Ada"}, variables={"v": "{{first_name}}"})
        for _ in range(3000):
            template = "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 60)))
            render(template, context)
            render(template, context, mode="html")

    def test_every_hostile_value_in_every_hostile_template(self):
        """The cross product, exhaustively — no engine, so no combination evaluates."""
        for template_value, field_value in itertools.product(HOSTILE_VALUES, HOSTILE_VALUES):
            rendered = render(f"{template_value}{{{{v}}}}", ctx(variables={"v": field_value}))
            assert rendered.endswith(field_value)


@pytest.mark.django_db
class TestContextForAContact:
    def test_system_fields_come_from_the_contact(self, tenancy):
        contact = contact_for(tenancy.workspace, email="ada@example.test", phone="+15550001111")
        context = context_for(contact)
        assert render("{{first_name}} {{last_name}} {{email}} {{phone}}", context) == (
            "Ada Lovelace ada@example.test +15550001111"
        )

    def test_custom_fields_come_by_name(self, tenancy):
        contact = contact_for(tenancy.workspace)
        field = create_custom_field(tenancy.workspace, name="Order Number", field_type="text")
        set_field_value(contact, field, "A-1000")

        assert render("{{Order Number}}", context_for(contact)) == "A-1000"
        assert render("{{order number}}", context_for(contact)) == "A-1000"

    def test_a_typed_custom_field_renders_through_its_stored_type(self, tenancy):
        contact = contact_for(tenancy.workspace)
        field = create_custom_field(tenancy.workspace, name="Spend", field_type="number")
        set_field_value(contact, field, "42.50")

        assert render("{{spend}}", context_for(contact)) == "42.5"

    def test_a_hostile_custom_field_value_stays_inert(self, tenancy):
        """The end-to-end case: hostile text through the CRM, out through a node."""
        contact = contact_for(tenancy.workspace, first_name="{{ 7*7 }}")
        field = create_custom_field(tenancy.workspace, name="Note", field_type="text")
        set_field_value(contact, field, "<script>alert(1)</script>")

        context = context_for(contact)
        assert render("{{first_name}} {{note}}", context) == "{{ 7*7 }} <script>alert(1)</script>"
        assert "<script>" not in render("{{note}}", context, mode="html")

    def test_no_contact_is_allowed(self):
        assert render("{{code}}", context_for(None, {"code": "9"})) == "9"


class TestUrlMode:
    """SPEC §11.7's External Request URL (L4-E).

    The claim is narrow and worth stating exactly: a substituted value becomes
    **one** URL component and cannot become a second one. Every character that
    separates components — ``/``, ``?``, ``#``, ``&``, ``=``, ``:`` — is encoded
    out of the value, and none of them is touched in the template.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("plain", "plain"),
            ("../../admin", "..%2F..%2Fadmin"),
            ("a?b=c", "a%3Fb%3Dc"),
            ("a&b", "a%26b"),
            ("a#frag", "a%23frag"),
            ("host:8000", "host%3A8000"),
            ("with space", "with%20space"),
            ("a+b", "a%2Bb"),
            ("Grüße", "Gr%C3%BC%C3%9Fe"),
            ("@evil.test", "%40evil.test"),
        ],
    )
    def test_a_value_becomes_one_component(self, value, expected):
        rendered = render("https://api.test/x/{{first_name}}", ctx(system={"first_name": value}), mode="url")
        assert rendered == f"https://api.test/x/{expected}"

    def test_the_template_itself_is_left_alone(self):
        """Encoding the template would percent-encode the ``://`` out of the address."""
        template = "https://api.test/orders?id={{first_name}}&format=json"
        rendered = render(template, ctx(system={"first_name": "42"}), mode="url")
        assert rendered == "https://api.test/orders?id=42&format=json"

    @pytest.mark.parametrize("hostile", HOSTILE_VALUES)
    def test_no_hostile_value_survives_as_url_syntax(self, hostile):
        rendered = render("https://api.test/{{first_name}}", ctx(system={"first_name": hostile}), mode="url")
        assert rendered.startswith("https://api.test/")
        for character in ("/", "?", "#", "&", ":", "@"):
            assert character not in rendered[len("https://api.test/") :]

    def test_an_unknown_token_still_renders_empty(self):
        assert render("https://api.test/{{nope}}", ctx(), mode="url") == "https://api.test/"


class TestRenderJson:
    def test_structure_is_preserved_and_strings_are_rendered(self):
        context = ctx(system={"first_name": "Ada"}, variables={"key": "X-Trace"})
        document = {"{{key}}": ["{{first_name}}", 7, None, True, {"n": "{{first_name}}"}]}

        assert render_json(document, context) == {"X-Trace": ["Ada", 7, None, True, {"n": "Ada"}]}

    def test_deep_nesting_is_bounded(self):
        document: object = "{{first_name}}"
        for _ in range(40):
            document = [document]
        assert render_json(document, ctx(system={"first_name": "Ada"})) is not None
