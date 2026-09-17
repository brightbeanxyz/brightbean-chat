"""No internal vocabulary reaches a reader.

Principle 5 of the redesign is "no ids or error codes in the UI", and it kept
being broken in the same two ways, each of which shipped:

- a **placeholder** left in the navigation, so a settings row or a tab strip
  offered a page whose whole content was "Templates is not built yet. Lands with
  issue #redesign." An issue number, to a customer;
- a **code** printed under a sentence that already said the same thing in
  English — ``flow_triggers_all_disabled`` beneath "Every trigger on this flow
  is switched off".

Both are invisible to every other test in the suite: the page renders, the
status is 200, and nothing is wrong except what it says. So they are checked
here, by rendering and reading.

Scope is deliberately narrow. These are not a style guide — they are the
specific leaks that reached production, written so that the same leak cannot
reach it twice.
"""

import re
from pathlib import Path

import pytest

from apps.common.context_processors import NavGroup

TEMPLATES = Path(__file__).parents[3] / "templates"

#: Strings that are internal vocabulary wherever they appear in rendered copy.
#: Each carries the reason, which is printed on failure so the fix is obvious.
BANNED = {
    r"issue #": "an issue number means nothing to a reader; say what they can do instead",
    r"SPEC §": "a spec section is a note to ourselves, not to the person reading the page",
    r"\bTODO\b": "a TODO on a page is a promise nobody is tracking",
    r"\bFIXME\b": "same",
}

#: Templates allowed to use developer vocabulary, because their reader IS a
#: developer following a third party's own documentation. Meta, Twilio and
#: Resend all say "webhook" and "endpoint"; renaming those here would make the
#: instructions harder to follow, not easier.
DEVELOPER_PAGES = (
    "api/",
    "credentials/",
    "channels/",
)


def _visible(html: str) -> str:
    """Everything a reader sees, with markup, scripts and comments removed."""
    from html.parser import HTMLParser

    class Visible(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.out: list[str] = []
            self.skip = 0

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in ("script", "style"):
                self.skip += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in ("script", "style") and self.skip:
                self.skip -= 1

        def handle_data(self, data: str) -> None:
            if not self.skip and data.strip():
                self.out.append(data.strip())

    parser = Visible()
    parser.feed(html)
    return " ".join(parser.out)


def _template_bodies() -> list[tuple[Path, str]]:
    """Every template, with Django comments stripped.

    Comments are stripped because they are where the *reasons* live, and the
    reasons name issues and spec sections freely — that is the point of them.
    """
    comment = re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}", re.S)
    return [(path, comment.sub(" ", path.read_text())) for path in sorted(TEMPLATES.rglob("*.html"))]


class TestNoInternalVocabularyIsRendered:
    @pytest.mark.parametrize(("pattern", "why"), list(BANNED.items()))
    def test_no_template_renders_it(self, pattern: str, why: str) -> None:
        offenders = []
        for path, body in _template_bodies():
            for line in _visible(body).splitlines():
                if re.search(pattern, line):
                    offenders.append(f"{path.name}: {line.strip()[:90]}")

        assert offenders == [], f"{why}. Found: {'; '.join(offenders)}"

    def test_no_template_prints_a_validation_code_beside_its_message(self) -> None:
        """Codes travel as attributes so support can quote them; not as text.

        `flow_problem_code` and the builder's rail both learned this the same
        way, so the rule is checked rather than remembered.
        """
        offenders = [
            path.name
            for path, body in _template_bodies()
            if re.search(r">\s*\{\{\s*(issue|problem)\.code\s*\}\}", body)
        ]

        assert offenders == [], f"These print an error code as visible text: {offenders}"


class TestNoInternalVocabularyReachesTheBuilder:
    """The flow builder's copy does not come from templates.

    It comes from the node registry and the JSON Schema, through
    ``static/flows/flow-schema.json``, and the template sweep above cannot see
    any of it. That is how "SPEC §11.1. Waits when buttons or quick replies are
    present" ended up as the description under "Send Message" in the Add a step
    menu, in a release whose copy pass had already run.

    The artefact is checked rather than the Python because the artefact is what
    actually ships to the browser: a description fixed in ``nodes.py`` and not
    re-exported is still wrong on the page.
    """

    @staticmethod
    def _strings(value: object, path: str = "") -> list[tuple[str, str]]:
        if isinstance(value, dict):
            return [
                found
                for key, item in value.items()
                for found in TestNoInternalVocabularyReachesTheBuilder._strings(item, f"{path}.{key}")
            ]
        if isinstance(value, list):
            return [
                found
                for index, item in enumerate(value)
                for found in TestNoInternalVocabularyReachesTheBuilder._strings(item, f"{path}[{index}]")
            ]
        return [(path, value)] if isinstance(value, str) else []

    def _artefact(self) -> dict:
        import json

        path = Path(__file__).parents[3] / "static" / "flows" / "flow-schema.json"
        return json.loads(path.read_text())

    @pytest.mark.parametrize(("pattern", "why"), list(BANNED.items()))
    def test_no_string_in_the_schema_artefact_carries_it(self, pattern: str, why: str) -> None:
        offenders = [
            f"{where}: {text[:70]}" for where, text in self._strings(self._artefact()) if re.search(pattern, text)
        ]

        assert offenders == [], f"{why}. Run `make schema` after fixing. Found: {'; '.join(offenders)}"

    def test_no_node_type_describes_itself_in_developer_terms(self) -> None:
        """Labels and descriptions are what the Add a step menu is made of."""
        offenders = []
        for spec in self._artefact()["x-brightbean"]["node_types"]:
            for field in ("label", "description"):
                text = spec.get(field, "")
                if re.search(r"SPEC|SECURITY-BASELINE|ROADMAP|runtime is|terminal in-graph", text, re.I):
                    offenders.append(f"{spec['type']}.{field}: {text[:70]}")

        assert offenders == [], f"These are read by somebody choosing a step: {offenders}"

    def test_the_artefact_matches_the_registry(self) -> None:
        """A description fixed in Python and not exported is still wrong on the page."""
        from apps.flows.schema.nodes import NODE_TYPES

        # NODE_TYPES is keyed by type, not a list.
        registry = {spec.type: (spec.label, spec.description) for spec in NODE_TYPES.values()}
        shipped = {
            spec["type"]: (spec["label"], spec.get("description", ""))
            for spec in self._artefact()["x-brightbean"]["node_types"]
        }

        assert shipped == registry, "static/flows/flow-schema.json is stale; run `make schema`."


class TestNoInternalVocabularyInValidationMessages:
    """The sentences somebody reads when their flow will not publish.

    These are Python strings, so neither the template sweep nor the artefact
    sweep sees them — and they were the worst offenders in the product: "Node
    'n7' does not expose 'btn:x'", "exactly one may (SPEC §9.1)". A person
    reading those has a flow that will not go live and no idea what to do.

    Checked structurally rather than by rendering, because reaching every one of
    them through the UI needs a broken graph per branch.
    """

    #: Words that name the data structure rather than the thing on screen. A
    #: step is a step; an edge is a connection; a handle is a path out of a step.
    INTERNAL = re.compile(r"\b(node|edge|handle|graph|entry node|terminal|annotation|runtime)\b", re.I)

    def _messages(self) -> list[tuple[str, str]]:
        """Every ``Issue`` message, read out of the source as literal text."""
        import ast

        found: list[tuple[str, str]] = []
        for path in (
            Path(__file__).parents[2] / "flows" / "schema" / "validation.py",
            Path(__file__).parents[2] / "flows" / "schema" / "jsonschema.py",
            Path(__file__).parents[2] / "flows" / "triggers" / "readiness.py",
        ):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "message":
                        continue
                    text = " ".join(
                        part.value
                        for part in ast.walk(keyword.value)
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    )
                    if text.strip():
                        found.append((f"{path.name}:{node.lineno}", text))
        return found

    def test_there_are_some_to_check(self) -> None:
        """Guards the guard: an AST change that finds nothing must not pass."""
        assert len(self._messages()) >= 10

    @pytest.mark.parametrize(("pattern", "why"), list(BANNED.items()))
    def test_none_cites_an_internal_document(self, pattern: str, why: str) -> None:
        offenders = [f"{where}: {text[:70]}" for where, text in self._messages() if re.search(pattern, text)]

        assert offenders == [], f"{why}. Found: {'; '.join(offenders)}"

    def test_none_names_the_data_structure_instead_of_the_thing_on_screen(self) -> None:
        offenders = [f"{where}: {text[:80]}" for where, text in self._messages() if self.INTERNAL.search(text)]

        assert offenders == [], (
            "These name the graph's vocabulary rather than the builder's. A reader has steps, "
            f"connections and paths, not nodes, edges and handles: {offenders}"
        )


class TestNoNavigationPointsAtAPlaceholder:
    def test_every_nav_row_leads_somewhere_real(self) -> None:
        """A row in the nav is a promise that there is a page behind it.

        The flow-template gallery shipped as a tab pointing at a stub for
        exactly one release, and it was found by a customer rather than by the
        suite. Placeholders may exist; nothing may advertise one.
        """
        from apps.common import context_processors
        from config import urls as config_urls

        stubbed = {name for _route, name, *_rest in config_urls._WORKSPACE_STUBS}
        stubbed |= {name for _route, name, *_rest in config_urls._GLOBAL_STUBS}

        # Every NavGroup list in the module, found rather than listed. The first
        # version of this test named MAIN_NAV and SETTINGS_NAV, and so missed
        # FLOWS_TABS — which is the one the dead Templates tab was actually in.
        advertised = {
            item.url_name
            for value in vars(context_processors).values()
            if isinstance(value, list) and value and all(isinstance(g, NavGroup) for g in value)
            for group in value
            for item in group.items
        }
        assert advertised, "found no navigation to check; this test has stopped testing anything"

        assert advertised & stubbed == set(), (
            "These navigation rows point at placeholder pages, so they promise a page that "
            f"answers 'not ready yet': {sorted(advertised & stubbed)}. Remove the row until "
            "the page exists, or build the page."
        )
