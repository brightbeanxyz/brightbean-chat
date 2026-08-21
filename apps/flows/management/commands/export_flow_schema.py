"""Write (or verify) the generated flow-schema artefact.

    python manage.py export_flow_schema           # regenerate
    python manage.py export_flow_schema --check   # fail if the committed copy is stale

``--check`` is what makes the committed file trustworthy: ``make frontend`` runs
the generator before the bundle is built, and
``apps/flows/tests/test_export.py`` runs the same comparison in the suite CI
already has, so a registry change that forgets the artefact is a red build
rather than a builder rendering last week's panels.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.flows.schema import artifact_path, serialize


class Command(BaseCommand):
    help = "Generate static/flows/flow-schema.json from the node registry."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Do not write; exit non-zero if the committed artefact differs.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = artifact_path()
        expected = serialize()

        if options["check"]:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != expected:
                raise CommandError(
                    f"{path} is out of date. Run `make schema` (or "
                    f"`python manage.py export_flow_schema`) and commit the result."
                )
            self.stdout.write(self.style.SUCCESS(f"{path} is up to date."))
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {path} ({len(expected)} bytes)."))
