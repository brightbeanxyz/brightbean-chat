"""Reading a file's attributes off a field that is not a file is silent.

``FileField`` hands a template a ``FieldFile``, so ``{{ asset.file.url }}``
works. Every other field hands it the value itself, and ``.url`` on a ``str``
is not an error in a Django template — the resolver fails the lookup and
substitutes ``string_if_invalid``, which is ``""``. No exception, no log line,
no failing test: the page renders and one attribute is gone.

This is a **port hazard** specifically. Models here are ported from BrightBean
Studio with deliberate deviations, and the templates come over with them. Where
the deviation is a field's *type*, the markup still reads as correct — the
attribute name is right, only the field behind it is a different kind of thing.
``Workspace.icon`` is the case that shipped: Studio has an ``ImageField``, this
repo has an eight-character ``CharField`` holding an emoji (the reason is in
``apps/workspaces/models.py``), and ``partials/_logo.html`` arrived rendering
``<img src="{{ workspace.icon.url }}">``. Every workspace with an icon set drew
a broken-image mark, and nothing anywhere said so.

The sweep is paired with its own regression fixtures, for the reason
``test_template_line_tags`` gives: a sweep that finds nothing looks exactly like
a sweep that cannot find anything, so the shape that shipped is fed back through
the scanner to prove it still bites, and the shapes that must **not** trip it
are pinned beside it.

What it can and cannot see
--------------------------
A template variable carries no type, so the only thing tying ``{{ x.y.url }}``
back to a model is the spelling of ``y``. That makes the rule in ``_verdict``
deliberately narrow — it judges a read only when every model declaring that name
declares it as a plain, non-relational field:

* **Never declared as a field at all** → skipped. ``{{ item.url }}`` over a list
  of nav dicts is not a model read.
* **Declared as a relation anywhere** → skipped. ``{{ delivery.webhook.url }}``
  crosses a foreign key into ``OutboundWebhook.url``, an ordinary ``URLField``,
  and from the name alone that is indistinguishable from a dict with a ``url``
  key. Skipping costs little: a relation's far side is a model, so the read is
  checked on its own when the chain is walked pairwise.
* **Declared only as file fields** → skipped. It is a file.
* **Declared as both a file field and something else** → reported, naming both.
  Nothing in the project does this today. It is reported rather than waved
  through because that collision is the one case where the name genuinely cannot
  be trusted, and silence there would hide the very bug this module exists for.

The residue is a known blind spot: a read off a name this project has no field
for cannot be caught here. ``apps/common/tests/test_shell.py`` covers the shell's
own templates by rendering them.
"""

import re
from collections.abc import Sequence
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import models

#: Both of Django's comment syntaxes. A comment can legitimately quote the
#: broken markup — this module's own subject does, in ``partials/_logo.html`` —
#: and ``{# #}`` is no more rendered than ``{% comment %}`` is, so both are
#: removed. Replaced by their own newlines, so line numbers still point at the
#: file.
COMMENT_RES = (
    re.compile(r"{%\s*comment\s*.*?%}.*?{%\s*endcomment\s*%}", re.DOTALL),
    re.compile(r"{#.*?#}", re.DOTALL),
)

#: Django's lexer, minus ``{# #}``, which the line above has already removed.
TAG_RE = re.compile(r"{{.*?}}|{%.*?%}", re.DOTALL)

#: A dotted variable path. The lookbehind keeps it off filenames in
#: ``{% static %}`` arguments and off quoted strings generally.
CHAIN_RE = re.compile(r"""(?<![\w.'"-])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+""")

#: The ``FieldFile`` API a template can reach. ``name`` is deliberately absent:
#: nearly every model in the project has a ``name`` field, so it says nothing
#: about whether the thing in front of it is a file.
FILE_ATTRS = frozenset({"url", "path", "size", "chunks", "storage"})


def _template_root() -> Path:
    return Path(settings.BASE_DIR) / "templates"


def _templates() -> list[Path]:
    return sorted(_template_root().rglob("*.html"))


def _fields_by_name() -> dict[str, list[tuple[type[models.Model], object]]]:
    """Every field name in the project, and everywhere it is declared.

    Read off the app registry rather than a hand-kept list, so a model added
    tomorrow is covered without anybody remembering this file.
    """
    index: dict[str, list[tuple[type[models.Model], object]]] = {}
    for model in apps.get_models():
        for field in model._meta.get_fields():
            index.setdefault(field.name, []).append((model, field))
    return index


def _verdict(declared: Sequence[tuple[type[models.Model], object]]) -> str | None:
    """``None`` when this name may not be judged; otherwise why it is an offence.

    The rules, and what each one buys, are in the module docstring.
    """
    if any(getattr(field, "related_model", None) is not None for _, field in declared):
        return None
    if all(isinstance(field, models.FileField) for _, field in declared):
        return None
    return ", ".join(sorted({f"{model.__name__} declares it a {type(field).__name__}" for model, field in declared}))


def _offenders(source: str, label: str) -> list[str]:
    index = _fields_by_name()
    body = source
    for pattern in COMMENT_RES:
        body = pattern.sub(lambda m: "\n" * m.group(0).count("\n"), body)
    found = []
    for tag in TAG_RE.finditer(body):
        line = body.count("\n", 0, tag.start()) + 1
        for chain in CHAIN_RE.findall(tag.group(0)):
            parts = chain.split(".")
            for holder, attr in zip(parts, parts[1:], strict=False):
                if attr not in FILE_ATTRS:
                    continue
                declared = index.get(holder)
                if not declared:
                    continue
                reason = _verdict(declared)
                if reason is None:
                    continue
                found.append(f"{label}:{line}: {chain} — {reason}")
    return found


def _sweep() -> list[str]:
    offenders: list[str] = []
    for path in _templates():
        offenders += _offenders(path.read_text(), str(path.relative_to(_template_root())))
    return offenders


class TestNoTemplateReadsAFileAttributeOffANonFileField:
    def test_the_sweep_is_clean(self) -> None:
        offenders = _sweep()

        assert offenders == [], (
            "These reads resolve to the empty string rather than raising, so the "
            "attribute silently disappears from the rendered page. Render the "
            "field's own value, or give the model a FileField: " + "; ".join(offenders)
        )

    def test_the_sweep_catches_the_shape_that_shipped(self) -> None:
        """``partials/_logo.html`` before the fix, fed back through the scanner.

        Asserted on the offence's head rather than the whole string: the tail
        enumerates every model declaring ``icon``, so a second one — a nav row,
        say — would otherwise break this test over a change that has nothing to
        do with the logo.
        """
        offenders = _offenders('<img src="{{ workspace.icon.url }}" alt="">', "partials/_logo.html")

        assert len(offenders) == 1, offenders
        assert offenders[0].startswith("partials/_logo.html:1: workspace.icon.url — ")
        assert "Workspace declares it a CharField" in offenders[0]

    def test_a_real_file_field_is_left_alone(self) -> None:
        """The other half: a sweep that flags everything is no guard either."""
        assert _offenders("{{ asset.thumbnail.url }}{{ run.file.size }}", "x.html") == []

    def test_a_read_through_a_relation_is_left_alone(self) -> None:
        """``webhook`` is a foreign key, so the name says nothing about the
        variable in front of it — and ``{{ ws.url }}`` over the switcher's
        dicts must stay legal whatever the loop variable is called."""
        assert _offenders("{{ delivery.webhook.url }}{{ workspace.url }}{{ folder.url }}", "x.html") == []

    def test_a_name_that_is_both_a_file_and_not_is_reported(self) -> None:
        """The one collision the name-based rule cannot resolve, so it speaks up."""
        both = [
            (apps.get_model("media_library.MediaAsset"), models.FileField(name="thumbnail")),
            (apps.get_model("workspaces.Workspace"), models.CharField(name="thumbnail")),
        ]

        assert _verdict(both) == "MediaAsset declares it a FileField, Workspace declares it a CharField"

    def test_both_comment_syntaxes_may_quote_the_broken_markup(self) -> None:
        """How the fix documents itself, in this file's own subject."""
        block = '{% comment %}\nPorted from Studio as <img src="{{ workspace.icon.url }}">.\n{% endcomment %}\n'
        inline = '{# Ported from Studio as <img src="{{ workspace.icon.url }}">. #}\n'

        assert _offenders(block + inline + "{{ workspace.icon }}\n", "partials/_logo.html") == []

    def test_a_failure_names_the_template_uniquely(self) -> None:
        """The label ``_sweep`` builds has to identify one file.

        Nine templates in the tree are called list.html, five settings.html and
        four detail.html, so a basename would leave the reader grepping for the
        line the guard exists to point at.
        """
        labels = [str(path.relative_to(_template_root())) for path in _templates()]

        assert len(set(labels)) == len(labels)
        assert len({Path(label).name for label in labels}) < len(labels), (
            "basenames are unique now, so the relative path is no longer load-bearing"
        )
