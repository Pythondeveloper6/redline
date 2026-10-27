"""``flask redline ...``: the command group registered on the app by ``init_app``.

Each subcommand is one decorated function below; adding one (``scan``, ``scenario``) means
adding one more function to the group.
"""

from __future__ import annotations

import click
from flask import current_app
from flask.cli import AppGroup

__all__ = ["redline_cli"]

redline_cli = AppGroup("redline", help="Manage Redline's recorded traces.")


def _runtime():
    runtime = current_app.extensions.get("redline")
    if runtime is None:  # pragma: no cover - the group is only registered by init_app
        raise click.ClickException("Redline is not initialised on this app.")
    return runtime


@redline_cli.command("clear")
@click.option("--yes", "-y", "--noinput", is_flag=True, help="Do not ask for confirmation.")
def clear_command(yes: bool) -> None:
    """Delete every recorded trace."""
    store = _runtime().store
    store.flush(5.0)
    if not yes and not click.confirm(f"Delete all {store.count()} Redline traces?"):
        click.echo("Cancelled.")
        return
    click.echo(f"Deleted {store.clear()} trace(s).")


@redline_cli.command("prune")
@click.option(
    "--max-age-days", type=float, default=None, help="Delete older traces; 0 disables the rule."
)
@click.option("--max-rows", type=int, default=None, help="Keep at most this many; 0 disables.")
def prune_command(max_age_days: float | None, max_rows: int | None) -> None:
    """Apply retention now: drop traces older than MAX_AGE_DAYS and the oldest beyond MAX_ROWS.

    Defaults to the max_age_days / max_rows settings. Use ``clear`` to delete everything.
    """
    runtime = _runtime()
    if max_age_days is not None and max_age_days < 0:
        raise click.UsageError("--max-age-days must not be negative.")
    if max_rows is not None and max_rows < 0:
        raise click.UsageError("--max-rows must not be negative.")
    settings, store = runtime.settings, runtime.store
    store.flush(5.0)
    deleted = store.prune(
        max_age_s=settings.max_age_s if max_age_days is None else max_age_days * 86_400.0,
        max_rows=settings.max_rows if max_rows is None else max_rows,
    )
    click.echo(f"Pruned {deleted} trace(s); {store.count()} left.")
