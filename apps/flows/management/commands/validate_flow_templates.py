"""Check every template in ``flow-templates/`` against the importer.

    python manage.py validate_flow_templates

Run it before opening a pull request that adds one. It is the same call the
upload path makes — size cap, JSON parse, depth cap, envelope schema with
unknown keys rejected, then every graph through
``apps.flows.schema.validate_graph`` and every trigger config through
``apps.flows.triggers.validation.validate_config`` — so a template that passes
here is a template the wizard will accept.

``apps/flows/tests/test_portability_library.py`` runs the same check inside the
suite CI already has, and goes one step further by importing each template into
a clean workspace. This command exists so a contributor gets the same answer
without running pytest.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.flows.portability.library import library_path, read_template, template_paths


class Command(BaseCommand):
    help = "Validate every flow template in flow-templates/ against the importer."

    def handle(self, *args: Any, **options: Any) -> None:
        paths = template_paths()
        if not paths:
            raise CommandError(f"No templates found in {library_path()}.")

        failed = 0
        for path in paths:
            document, issues = read_template(path)
            if document is None:
                failed += 1
                self.stderr.write(f"{path.name}: rejected")
                for issue in issues[:10]:
                    self.stderr.write(f"    {issue.path or '-'}: {issue.message}")
                continue
            flows = document["flows"]
            triggers = sum(len(flow["triggers"]) for flow in flows)
            self.stdout.write(
                f"{path.name}: ok — {len(flows)} flow(s), {triggers} trigger(s), entry {document['entry']!r}"
            )

        if failed:
            raise CommandError(f"{failed} of {len(paths)} template(s) are not importable.")
        self.stdout.write(self.style.SUCCESS(f"All {len(paths)} template(s) validate."))
