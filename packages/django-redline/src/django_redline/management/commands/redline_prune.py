from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_redline.runtime import get_runtime


class Command(BaseCommand):
    help = (
        "Apply Redline's retention now: delete traces older than MAX_AGE_DAYS and the "
        "oldest beyond MAX_ROWS (or the values given here)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-age-days", type=float, default=None)
        parser.add_argument("--max-rows", type=int, default=None)

    def handle(self, *args: Any, **options: Any) -> None:
        rt = get_runtime()
        days = options["max_age_days"]
        rows = options["max_rows"]
        if days is not None and days < 0:
            raise CommandError("--max-age-days must not be negative.")
        if rows is not None and rows < 0:
            raise CommandError("--max-rows must not be negative.")
        rt.store.flush(5.0)
        deleted = rt.store.prune(
            max_age_s=rt.settings.max_age_s if days is None else days * 86_400.0,
            max_rows=rt.settings.max_rows if rows is None else rows,
        )
        self.stdout.write(
            self.style.SUCCESS(f"Pruned {deleted} trace(s); {rt.store.count()} left.")
        )
