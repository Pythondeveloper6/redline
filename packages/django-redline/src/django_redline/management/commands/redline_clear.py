from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from django_redline.runtime import get_runtime


class Command(BaseCommand):
    help = "Delete every recorded Redline trace."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not ask for confirmation.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        store = get_runtime().store
        store.flush(5.0)
        if options["interactive"]:
            answer = input(f"Delete all {store.count()} Redline traces? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                self.stdout.write("Cancelled.")
                return
        deleted = store.clear()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} trace(s)."))
