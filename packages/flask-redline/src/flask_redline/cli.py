"""``flask redline ...``: the command group registered on the app by ``init_app``.

Each subcommand is one decorated function below: ``clear``, ``prune``, ``scan`` (static N+1
scan of the Jinja templates) and ``scenario`` (a load-test scenario from ``app.url_map``).
"""

from __future__ import annotations

import json
import os
from typing import Any

import click
from flask import current_app
from flask.cli import AppGroup

__all__ = ["redline_cli"]

redline_cli = AppGroup("redline", help="Redline: traces, template scan and load scenarios.")


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


@redline_cli.command("scan")
@click.option("--json", "as_json", is_flag=True, help="Print the findings as JSON.")
@click.option("--fail", is_flag=True, help="Exit with status 1 when anything is found.")
def scan_command(as_json: bool, fail: bool) -> None:
    """Find likely N+1 patterns in the Jinja templates (loops reading related objects)."""
    from redline_core.scan import format_table, scan_jinja_loader

    app = current_app._get_current_object()  # type: ignore[attr-defined]
    warnings: list[str] = []
    findings = scan_jinja_loader([app, *app.iter_blueprints()], warnings=warnings)
    for w in warnings:
        click.echo(f"warning: {w}", err=True)
    if as_json:
        click.echo(json.dumps([f.to_dict() for f in findings], indent=2))
    elif findings:
        click.echo(format_table(findings, relative_to=os.getcwd()))
    else:
        click.echo("No N+1 patterns found in the templates.")
    if fail and findings:
        raise SystemExit(1)


def _routes(app: Any, prefix: str) -> list[tuple[str, str, str | None]]:
    """``(method, template, concrete path or None)`` for every GET rule of the app.

    The template is ``rule.rule`` – what the middleware records as ``Trace.route`` (without
    a script root) – so ``from_routes`` can fill converters from recent traces.
    """
    out = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if "GET" not in (rule.methods or ()) or rule.endpoint == "static":
            continue
        if rule.endpoint.startswith("redline.") or rule.rule.startswith(prefix):
            continue
        out.append(("GET", rule.rule, None if rule.arguments else rule.rule))
    return out


@redline_cli.command("scenario")
@click.option("-o", "--output", "output", default=None, help="Write to FILE (.json or .toml).")
@click.option(
    "--base-url", default="http://127.0.0.1:5000", show_default=True, help="Target server."
)
@click.option("--force", is_flag=True, help="Overwrite an existing FILE.")
def scenario_command(output: str | None, base_url: str, force: bool) -> None:
    """Write a load-test scenario for `redline load` from the app's GET routes.

    Routes without parameters get weight 1; parameterised ones are filled with a concrete
    path from recently recorded traffic, and left out when there is none.
    """
    from redline_core.load.generate import dump, from_routes
    from redline_core.load.scenario import parse_scenario

    runtime = _runtime()
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    store = runtime.store
    try:
        store.flush(5.0)
        data = from_routes(
            _routes(app, runtime.prefix),
            store=store,
            base_url=base_url,
            name=f"{app.name} routes",
            mount_prefix=runtime.prefix,
        )
        parse_scenario(data)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    text = dump(data, output)
    if output in (None, "-"):
        click.echo(text, nl=False)
        return
    if os.path.exists(output) and not force:
        raise click.ClickException(f"{output} exists (use --force to overwrite).")
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(text)
    click.echo(f"Wrote {output}: {len(data['requests'])} request(s), base_url {data['base_url']}.")
