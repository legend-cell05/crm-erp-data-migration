"""Command-line interface.

One command per operation in :mod:`keystone.pipeline`, so the same entry
points are used locally, inside Docker, in CI and by whatever schedules the
migration. Nothing here contains migration logic; if a command needs a
decision made, the decision belongs in the pipeline and the command belongs in
this file only to print it.

Exit codes: 0 success, 1 handled failure, 2 misuse, 3 blocked by a gate. The
last one is separate on purpose: a blocked migration is not a crash, and a
scheduler should be able to tell the difference without parsing text.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from keystone import __version__, pipeline
from keystone.config import get_settings
from keystone.db.engine import check_connection
from keystone.db.schema import drop_schemas, table_counts
from keystone.dryrun.profile import profile_entity
from keystone.exceptions import KeystoneError, MigrationBlocked
from keystone.load.client import atlas_client
from keystone.load.crosswalk import CrosswalkStore
from keystone.load.rejects import RejectStore
from keystone.logging_config import configure_logging
from keystone.mapping.functions import registered_transforms
from keystone.mapping.loader import load_mapping_set
from keystone.mapping.validations import registered_rules
from keystone.run_record import recent_runs

app = typer.Typer(
    add_completion=False,
    help="keystone -- declarative CRM migration from Arcadia to Atlas Cloud (synthetic data).",
)
console = Console()

EXIT_FAILURE = 1
EXIT_MISUSE = 2
EXIT_BLOCKED = 3


def _bootstrap() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)


def _fail(message: str, code: int = EXIT_FAILURE) -> None:
    console.print(f"[bold red]FAILED[/bold red] {message}")
    raise typer.Exit(code=code)


def _require_database() -> None:
    if not check_connection(get_settings(), retries=5):
        _fail("database unreachable -- check .env and that PostgreSQL is running")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    _bootstrap()
    if version:
        console.print(f"keystone {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Check configuration, database and target connectivity."""
    settings = get_settings()

    table = Table(title="keystone doctor", header_style="bold")
    table.add_column("Check")
    table.add_column("Value")
    table.add_row("version", __version__)
    table.add_row("database", settings.safe_dsn)
    table.add_row("target", settings.target_base_url)
    table.add_row("mappings", str(settings.mapping_dir))
    table.add_row("data", str(settings.data_dir))
    table.add_row("batch size", str(settings.batch_size))
    table.add_row("reject ceiling", f"{settings.max_reject_rate_pct:.2f}%")

    database_ok = check_connection(settings, retries=2)
    table.add_row("database reachable", "[green]yes[/green]" if database_ok else "[red]no[/red]")

    target_ok = False
    try:
        with atlas_client(settings) as client:
            client.metadata()
            target_ok = True
    except Exception as exc:
        table.add_row("target error", str(exc)[:80])
    table.add_row("target reachable", "[green]yes[/green]" if target_ok else "[red]no[/red]")

    mapping_ok = False
    try:
        mapping_set = load_mapping_set(settings)
        table.add_row("mapping version", mapping_set.version)
        table.add_row("entities", ", ".join(mapping_set.order))
        mapping_ok = True
    except KeystoneError as exc:
        table.add_row("mapping error", str(exc)[:80])

    console.print(table)
    if not (database_ok and mapping_ok):
        raise typer.Exit(code=EXIT_FAILURE)


@app.command()
def mappings() -> None:
    """List the mapping set, its order and its fingerprint."""
    try:
        mapping_set = load_mapping_set(get_settings())
    except KeystoneError as exc:
        _fail(str(exc))

    table = Table(title=f"Mapping set {mapping_set.version}", header_style="bold")
    for column in ("#", "entity", "file version", "source", "fields", "validations", "depends on"):
        table.add_column(column)
    for index, name in enumerate(mapping_set.order, start=1):
        mapping = mapping_set[name]
        source = (
            f"{mapping.source.schema_name}.{mapping.source.table}"
            if mapping.source.kind == "sql"
            else str(mapping.source.path_glob)
        )
        table.add_row(
            str(index),
            mapping.entity,
            mapping.version,
            source,
            str(len(mapping.fields)),
            str(len(mapping.validations)),
            ", ".join(mapping.depends_on) or "-",
        )
    console.print(table)
    console.print(
        "[dim]The fingerprint covers every mapping file and lookup table. "
        "Editing one invalidates the dry-run that approved it.[/dim]"
    )


@app.command()
def registry() -> None:
    """List the transformations and validation rules a mapping may use."""
    table = Table(title="Available in a mapping file", header_style="bold")
    table.add_column("transforms")
    table.add_column("validation rules")
    transforms = registered_transforms()
    rules = registered_rules()
    for index in range(max(len(transforms), len(rules))):
        table.add_row(
            transforms[index] if index < len(transforms) else "",
            rules[index] if index < len(rules) else "",
        )
    console.print(table)


@app.command()
def profile(
    entity: Annotated[str | None, typer.Option(help="Restrict to one entity.")] = None,
    min_empty_pct: Annotated[
        float, typer.Option(help="Only show columns at least this empty.")
    ] = 0.0,
) -> None:
    """Profile the source columns the mapping reads."""
    _require_database()
    settings = get_settings()
    try:
        mapping_set = load_mapping_set(settings)
        entities = [entity] if entity else list(mapping_set.order)
        table = Table(title="Source profile", header_style="bold")
        for column in ("entity", "column", "rows", "empty", "distinct", "examples"):
            table.add_column(
                column, justify="right" if column in {"rows", "empty", "distinct"} else "left"
            )
        for name in entities:
            for column_profile in profile_entity(mapping_set[name], settings):
                if column_profile.empty_pct < min_empty_pct:
                    continue
                table.add_row(
                    name,
                    column_profile.column,
                    f"{column_profile.row_count:,}",
                    f"{column_profile.empty_pct:.1f}%",
                    f"{column_profile.distinct_count:,}",
                    ", ".join(column_profile.samples[:3])[:60],
                )
        console.print(table)
    except KeystoneError as exc:
        _fail(str(exc))


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


@app.command()
def seed() -> None:
    """Generate the simulated legacy CRM and its nightly exports."""
    _require_database()
    try:
        result = pipeline.seed(get_settings())
    except KeystoneError as exc:
        _fail(str(exc))

    table = Table(title="Simulated Arcadia CRM", header_style="bold")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for name, count in result["rows"].items():
        table.add_row(name, f"{count:,}")
    console.print(table)

    defects = Table(title="Defects injected on purpose", header_style="bold")
    defects.add_column("kind")
    defects.add_column("records", justify="right")
    for kind, count in result["defects"].items():
        defects.add_row(kind, f"{count:,}")
    console.print(defects)
    console.print(f"[bold green]OK[/bold green] {result['export_files']} activity export file(s)")


@app.command(name="init-db")
def init_db() -> None:
    """Create the schemas and tables. Safe to re-run."""
    _require_database()
    try:
        applied = pipeline.init_db(get_settings())
    except KeystoneError as exc:
        _fail(str(exc))
    console.print(f"[bold green]OK[/bold green] {len(applied)} schema file(s) applied")


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def _print_dry_run(report: Any) -> None:
    table = Table(title=f"Impact -- mapping {report.mapping_version}", header_style="bold")
    for column in ("entity", "read", "create", "update", "skip", "reject", "rate"):
        table.add_column(column, justify="right" if column != "entity" else "left")
    for plan in report.plans:
        table.add_row(
            plan.entity,
            f"{plan.records_read:,}",
            f"{plan.actions['create']:,}",
            f"{plan.actions['update']:,}",
            f"{plan.actions['skip']:,}",
            f"{plan.rejected:,}",
            f"{plan.reject_rate_pct:.2f}%",
        )
    table.add_section()
    table.add_row(
        "[bold]total[/bold]",
        f"[bold]{report.records_read:,}[/bold]",
        "",
        "",
        "",
        f"[bold]{report.records_rejected:,}[/bold]",
        f"[bold]{report.reject_rate_pct:.2f}%[/bold]",
    )
    console.print(table)

    if report.rejection_causes:
        causes = Table(title="Why records would be rejected", header_style="bold")
        for column in ("entity", "field", "rule", "records", "example"):
            causes.add_column(column, justify="right" if column == "records" else "left")
        for cause in report.rejection_causes[:10]:
            causes.add_row(
                cause["entity"],
                str(cause["field"] or "-"),
                str(cause["rule"] or "-"),
                f"{cause['records']:,}",
                cause["example_message"][:56],
            )
        console.print(causes)


@app.command(name="dry-run")
def dry_run_command() -> None:
    """Plan the migration and write the impact report. Writes nothing to the target."""
    _require_database()
    settings = get_settings()
    try:
        report = pipeline.dry_run(settings)
    except KeystoneError as exc:
        _fail(str(exc))

    _print_dry_run(report)
    console.print(
        Panel(
            f"Nothing was written to the target.\n"
            f"Report: {settings.report_dir / 'dry_run_latest.md'}",
            title="dry run",
            border_style="cyan",
        )
    )

    decision = pipeline.gate(settings)
    if decision.allowed:
        console.print(f"[bold green]GATE OPEN[/bold green] {decision.reason}")
    else:
        console.print(f"[bold yellow]GATE CLOSED[/bold yellow] {decision.reason}")


@app.command()
def gate() -> None:
    """Show whether a load would be allowed to start."""
    _require_database()
    try:
        decision = pipeline.gate(get_settings())
    except KeystoneError as exc:
        _fail(str(exc))
    if decision.allowed:
        console.print(f"[bold green]OPEN[/bold green] {decision.reason}")
        return
    console.print(f"[bold red]CLOSED[/bold red] {decision.reason}")
    raise typer.Exit(code=EXIT_BLOCKED)


def _print_load(summary: Any) -> None:
    table = Table(title=f"Load {summary.run_id}", header_style="bold")
    for column in (
        "entity",
        "read",
        "created",
        "updated",
        "unchanged",
        "skipped",
        "rejected",
        "batches",
        "retries",
    ):
        table.add_column(column, justify="right" if column != "entity" else "left")
    for report in summary.reports:
        table.add_row(
            report.entity,
            f"{report.records_read:,}",
            f"{report.created:,}",
            f"{report.updated:,}",
            f"{report.unchanged:,}",
            f"{report.skipped:,}",
            f"{report.rejected:,}",
            f"{report.batches:,}",
            f"{report.retries:,}",
        )
    console.print(table)
    console.print(
        f"status [bold]{summary.status}[/bold] in {summary.duration_seconds:.1f} s -- "
        f"{summary.created:,} created, {summary.updated:,} updated, "
        f"{summary.unchanged:,} unchanged, {summary.rejected:,} rejected"
    )


@app.command(name="load")
def load_command(
    entity: Annotated[list[str] | None, typer.Option(help="Restrict to these entities.")] = None,
    force: Annotated[bool, typer.Option(help="Load even if the dry-run gate is closed.")] = False,
) -> None:
    """Migrate into the target. Refuses to start without a matching dry-run."""
    _require_database()
    settings = get_settings()
    try:
        with atlas_client(settings) as client:
            summary = pipeline.load(client, settings, entities=entity, force=force)
    except MigrationBlocked as exc:
        console.print(f"[bold red]BLOCKED[/bold red] {exc}")
        console.print(
            "[dim]Run `keystone dry-run`, or pass --force to override deliberately.[/dim]"
        )
        raise typer.Exit(code=EXIT_BLOCKED) from exc
    except KeystoneError as exc:
        _fail(str(exc))
    _print_load(summary)


@app.command()
def migrate(
    force: Annotated[bool, typer.Option(help="Load even if the dry-run gate is closed.")] = False,
) -> None:
    """Dry-run, load and reconcile, in that order."""
    _require_database()
    settings = get_settings()
    try:
        report = pipeline.dry_run(settings)
        _print_dry_run(report)
        with atlas_client(settings) as client:
            summary = pipeline.load(client, settings, force=force)
            _print_load(summary)
            reconciliation = pipeline.reconcile_migration(client, settings)
    except MigrationBlocked as exc:
        console.print(f"[bold red]BLOCKED[/bold red] {exc}")
        raise typer.Exit(code=EXIT_BLOCKED) from exc
    except KeystoneError as exc:
        _fail(str(exc))
    _print_reconciliation(reconciliation)


def _print_reconciliation(report: Any) -> None:
    table = Table(title="Reconciliation", header_style="bold")
    for column in ("check", "entity", "expected", "observed", "status"):
        table.add_column(column)
    for result in report.results:
        colour = {"OK": "green", "WARN": "yellow", "MISMATCH": "red"}[result.status]
        table.add_row(
            result.name,
            result.entity,
            result.expected,
            result.observed,
            f"[{colour}]{result.status}[/{colour}]",
        )
    console.print(table)
    if report.passed:
        console.print("[bold green]OK[/bold green] source, crosswalk and target agree")
    else:
        console.print(f"[bold red]MISMATCH[/bold red] {len(report.mismatches)} check(s) disagree")


@app.command(name="reconcile")
def reconcile_command() -> None:
    """Compare source, crosswalk and target."""
    _require_database()
    settings = get_settings()
    try:
        with atlas_client(settings) as client:
            report = pipeline.reconcile_migration(client, settings)
    except KeystoneError as exc:
        _fail(str(exc))
    _print_reconciliation(report)
    if not report.passed:
        raise typer.Exit(code=EXIT_FAILURE)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@app.command()
def rejects(
    entity: Annotated[str | None, typer.Option(help="Restrict to one entity.")] = None,
    show_examples: Annotated[bool, typer.Option(help="Show example records.")] = False,
    limit: Annotated[int, typer.Option(help="Examples to show.")] = 10,
) -> None:
    """Show what did not migrate, and why."""
    _require_database()
    settings = get_settings()
    store = RejectStore(settings)
    try:
        summary = store.summary()
        counts = store.counts_by_status()
    except KeystoneError as exc:
        _fail(str(exc))

    if not summary:
        console.print("[green]No rejections recorded.[/green]")
        return

    table = Table(title="Rejections by cause", header_style="bold")
    for column in ("entity", "stage", "field", "rule", "status", "records", "attempts"):
        table.add_column(column, justify="right" if column in {"records", "attempts"} else "left")
    for row in summary:
        if entity and row["entity"] != entity:
            continue
        table.add_row(
            row["entity"],
            row["stage"],
            str(row["field"] or "-"),
            str(row["rule"] or "-"),
            row["status"],
            f"{row['records']:,}",
            str(row["max_attempts"]),
        )
    console.print(table)
    console.print(" ".join(f"{status}: {count:,}" for status, count in sorted(counts.items())))

    if show_examples:
        examples = Table(title="Examples", header_style="bold")
        for column in ("entity", "source id", "field", "message"):
            examples.add_column(column)
        for row in store.pending(entity, limit=limit):
            examples.add_row(
                row["entity"], row["source_id"], str(row["field"] or "-"), row["message"][:70]
            )
        console.print(examples)


@app.command()
def crosswalk(
    entity: Annotated[str | None, typer.Option(help="Restrict to one entity.")] = None,
    source_id: Annotated[str | None, typer.Option(help="Look up one legacy id.")] = None,
) -> None:
    """Show what became what."""
    _require_database()
    settings = get_settings()
    store = CrosswalkStore(settings)

    if source_id:
        if entity is None:
            _fail("--source-id needs --entity", code=EXIT_MISUSE)
            raise typer.Exit(code=EXIT_MISUSE)  # unreachable; narrows the type
        target_id = store.resolve(entity, source_id)
        if target_id is None:
            console.print(f"[yellow]{entity} {source_id} has not been migrated[/yellow]")
            raise typer.Exit(code=EXIT_FAILURE)
        console.print(f"{entity} [bold]{source_id}[/bold] -> [bold]{target_id}[/bold]")
        return

    table = Table(title="Crosswalk", header_style="bold")
    table.add_column("entity")
    table.add_column("records", justify="right")
    for name, count in store.counts().items():
        table.add_row(name, f"{count:,}")
    console.print(table)


@app.command()
def runs(limit: Annotated[int, typer.Option(help="How many runs to show.")] = 10) -> None:
    """Show the run history."""
    _require_database()
    try:
        history = recent_runs(limit, get_settings())
    except KeystoneError as exc:
        _fail(str(exc))

    table = Table(title="Runs", header_style="bold")
    for column in ("started", "kind", "mapping version", "status", "read", "written", "rejected"):
        table.add_column(
            column, justify="right" if column in {"read", "written", "rejected"} else "left"
        )
    for row in history:
        colour = {"SUCCESS": "green", "PARTIAL": "yellow", "FAILED": "red"}.get(
            row["status"], "white"
        )
        table.add_row(
            row["started_at"].strftime("%Y-%m-%d %H:%M:%S"),
            row["kind"],
            row["mapping_version"],
            f"[{colour}]{row['status']}[/{colour}]",
            f"{row['records_read']:,}",
            f"{row['records_written']:,}",
            f"{row['records_rejected']:,}",
        )
    console.print(table)


@app.command()
def tables() -> None:
    """Row counts for every table keystone owns or reads."""
    _require_database()
    table = Table(title="Row counts", header_style="bold")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for name, count in table_counts(get_settings()).items():
        table.add_row(name, f"{count:,}")
    console.print(table)


# ---------------------------------------------------------------------------
# The simulated target
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8080,
    reload: Annotated[bool, typer.Option(help="Reload on code changes.")] = False,
) -> None:
    """Run the simulated Atlas Cloud CRM."""
    import uvicorn

    settings = get_settings()
    console.print(f"[bold]Atlas Cloud (simulated)[/bold] on http://{host}:{port}")
    uvicorn.run(
        "keystone.target.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command(name="target-stats")
def target_stats() -> None:
    """Row counts in the target."""
    settings = get_settings()
    try:
        with atlas_client(settings) as client:
            metadata = client.metadata()
            table = Table(title="Atlas Cloud", header_style="bold")
            table.add_column("entity set")
            table.add_column("records", justify="right")
            for entity_set in metadata["load_order"]:
                table.add_row(entity_set, f"{client.count(entity_set):,}")
    except KeystoneError as exc:
        _fail(str(exc))
    console.print(table)


@app.command(name="export-report")
def export_report(
    path: Annotated[str | None, typer.Option(help="Where to write the JSON.")] = None,
) -> None:
    """Print the latest dry-run report as JSON."""
    _require_database()
    settings = get_settings()
    reports = sorted(settings.report_dir.glob("dry_run_*.json"))
    if not reports:
        _fail("no dry-run report found -- run `keystone dry-run` first")
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    if path:
        from pathlib import Path

        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"[bold green]OK[/bold green] written to {path}")
        return
    console.print_json(data=payload["totals"])


@app.command(name="reset")
def reset_command(
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask.")] = False,
    keep_legacy: Annotated[
        bool, typer.Option(help="Keep the legacy source; drop only the migration state.")
    ] = False,
) -> None:
    """Drop the schemas. Destroys the crosswalk."""
    settings = get_settings()
    dropped = (
        settings.migration_schema
        if keep_legacy
        else f"{settings.legacy_schema} and {settings.migration_schema}"
    )
    if not yes:
        confirmed = typer.confirm(
            f"Drop {dropped} on {settings.safe_dsn}? The crosswalk cannot be rebuilt.",
            default=False,
        )
        if not confirmed:
            console.print("Cancelled.")
            raise typer.Exit()
    try:
        drop_schemas(settings, include_legacy=not keep_legacy)
    except KeystoneError as exc:
        _fail(str(exc))
    console.print("[bold green]OK[/bold green] schemas dropped")


if __name__ == "__main__":  # pragma: no cover
    app()
