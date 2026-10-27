from __future__ import annotations

import json
import os
import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from django_redline.scan import scan_project
from redline_core.scan import format_table


class Command(BaseCommand):
    help = (
        "Statically scan templates for loops that read a relation off the loop variable "
        "(a likely N+1), and DRF serializers whose view queryset lacks the matching "
        "select_related/prefetch_related. Scans every template directory by default."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("paths", nargs="*", help="Template files or directories to scan.")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON.")
        parser.add_argument(
            "--fail", action="store_true", help="Exit with status 1 when anything is found."
        )
        parser.add_argument(
            "--dialect",
            choices=["django", "jinja"],
            default="django",
            help="Template syntax of the explicit paths (default django).",
        )
        parser.add_argument(
            "--no-drf", action="store_true", help="Skip the DRF serializer/queryset check."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        warnings: list[str] = []
        findings = scan_project(
            options["paths"] or None,
            dialect=options["dialect"],
            drf=not options["no_drf"],
            warnings=warnings,
        )
        if options["as_json"]:
            payload = {"findings": [f.to_dict() for f in findings], "warnings": warnings}
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            for w in warnings:
                self.stderr.write(f"warning: {w}")
            if findings:
                self.stdout.write(format_table(findings, relative_to=os.getcwd()))
                self.stdout.write("")
                self.stdout.write(self.style.WARNING(f"{len(findings)} possible N+1 finding(s)."))
            else:
                self.stdout.write(self.style.SUCCESS("No N+1 patterns found."))
        if findings and options["fail"]:
            sys.exit(1)
