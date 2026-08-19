from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from mapslead.campaign_exporter import CampaignExporter
from mapslead.config import DAILY_NEW_RECORD_LIMIT, DEFAULT_RUN_LIMIT, Settings
from mapslead.enrichment import WebsiteEnrichmentService
from mapslead.errors import MapsLeadError
from mapslead.exporter import Exporter
from mapslead.input_validation import (
    validate_language_code,
    validate_location_query,
    validate_search_query,
)
from mapslead.models import CampaignStatus, ExportPaths, ProgressEvent, RunStatus
from mapslead.provider import GosomDockerProvider
from mapslead.repository import SQLiteRepository
from mapslead.service import MapsLeadService, RequestedLimitError, ResumeNotAllowedError

PROVIDER_IMAGE = "gosom/google-maps-scraper"
DOCKER_UNAVAILABLE_MESSAGE = "Docker is unavailable. Install and start Docker, then retry."
PROVIDER_IMAGE_MISSING_MESSAGE = (
    f"Provider image is missing. Run: docker pull {PROVIDER_IMAGE}"
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
campaign_app = typer.Typer(no_args_is_help=True)
app.add_typer(campaign_app, name="campaign")
DATA_DIR_OPTION = typer.Option(
    None,
    "--data-dir",
    file_okay=False,
    dir_okay=True,
    writable=True,
    resolve_path=False,
    help="Directory for the SQLite database and provider working files.",
)
EXPORT_DIR_OPTION = typer.Option(
    None,
    "--export-dir",
    file_okay=False,
    dir_okay=True,
    writable=True,
    resolve_path=False,
    help="Directory for per-run CSV and JSON exports.",
)
BUSINESS_OPTION = typer.Option(..., "--business", help="Business type to search for.")
CAMPAIGN_OPTION = typer.Option(None, "--campaign", help="Campaign slug to scrape into.")
LIMIT_OPTION = typer.Option(
    DEFAULT_RUN_LIMIT,
    "--limit",
    min=1,
    help="Maximum number of new unique records to accept for this run.",
)
QUERY_OPTION = typer.Option(
    None,
    "--query",
    help="Search query alias to send to Google Maps. Repeat to batch multiple aliases.",
)
LOCATION_OPTION = typer.Option(
    ...,
    "--location",
    help="Location query to search in. Repeat to batch multiple cities or districts.",
)
LANGUAGE_OPTION = typer.Option(
    "en",
    "--language",
    help="Provider language code such as en or vi.",
)
EXPORT_RUN_ID_OPTION = typer.Option(..., "--run-id", help="Existing run identifier to export.")
REFRESH_ENRICHMENT_OPTION = typer.Option(
    False,
    "--refresh-enrichment",
    help="Force website enrichment even when a matching successful cache exists.",
)


class PrerequisiteError(MapsLeadError):
    """Raised when a required local operator dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class AppRuntime:
    settings: Settings
    repository: Any
    service: Any
    exporter: Any
    campaign_exporter: Any
    clock: Any
    prerequisite_checker: Any


@dataclass(frozen=True, slots=True)
class ScrapePlanItem:
    business: str
    location: str
    limit: int
    query: str | None
    language: str
    campaign_slug: str | None


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class OperatorPrerequisiteChecker:
    def __init__(
        self,
        *,
        run_command: Any = None,
    ) -> None:
        self._run_command = run_command or _run_command

    def check(self) -> None:
        try:
            docker_version = self._run_command(["docker", "version"])
        except OSError as exc:
            raise PrerequisiteError(DOCKER_UNAVAILABLE_MESSAGE) from exc

        if docker_version.returncode != 0:
            raise PrerequisiteError(DOCKER_UNAVAILABLE_MESSAGE)

        image_check = self._run_command(["docker", "image", "inspect", PROVIDER_IMAGE])
        if image_check.returncode != 0:
            raise PrerequisiteError(PROVIDER_IMAGE_MISSING_MESSAGE)


def build_service(settings: Settings) -> AppRuntime:
    repository = SQLiteRepository(settings)
    repository.initialize()
    exporter = Exporter(repository, settings)
    campaign_exporter = CampaignExporter(repository, settings)
    service = MapsLeadService(
        repository=repository,
        provider=GosomDockerProvider(),
        enricher=WebsiteEnrichmentService(),
        exporter=exporter,
    )
    return AppRuntime(
        settings=settings,
        repository=repository,
        service=service,
        exporter=exporter,
        campaign_exporter=campaign_exporter,
        clock=SystemClock(),
        prerequisite_checker=OperatorPrerequisiteChecker().check,
    )


@app.callback()
def main(
    ctx: typer.Context,
    data_dir: Path | None = DATA_DIR_OPTION,
    export_dir: Path | None = EXPORT_DIR_OPTION,
) -> None:
    ctx.obj = _resolve_settings(data_dir=data_dir, export_dir=export_dir)


@app.command()
def scrape(
    ctx: typer.Context,
    business: str | None = typer.Option(None, "--business", help="Business type to search for."),
    campaign: str | None = CAMPAIGN_OPTION,
    query: list[str] | None = QUERY_OPTION,
    location: list[str] = LOCATION_OPTION,
    language: str = LANGUAGE_OPTION,
    limit: int = LIMIT_OPTION,
    refresh_enrichment: bool = REFRESH_ENRICHMENT_OPTION,
) -> None:
    runtime = _runtime_for_context(ctx)
    if (business is None) == (campaign is None):
        _exit_with_message("exactly one of --business and --campaign is required", code=2)

    resolved_business = business
    if campaign is not None:
        try:
            resolved_business = runtime.repository.get_campaign(campaign).business_type
        except (MapsLeadError, KeyError) as exc:
            _exit_with_message(_message_for_exception(exc), code=1)

    assert resolved_business is not None
    try:
        plan = _build_scrape_plan(
            business=resolved_business,
            campaign_slug=campaign,
            queries=query,
            locations=location,
            language=language,
            limit=limit,
        )
    except ValueError as exc:
        _exit_with_message(_message_for_exception(exc), code=2)

    completed_count = 0
    launched_count = 0
    prerequisites_checked = False
    total_pairs = len(plan)
    for index, item in enumerate(plan, start=1):
        now = runtime.clock.now()
        remaining = runtime.repository.remaining_quota(now)
        if remaining <= 0:
            typer.echo(f"Batch stopped: daily quota exhausted before pair {index}/{total_pairs}.")
            _render_batch_summary(completed_count, total_pairs)
            return

        effective_limit = min(item.limit, remaining)
        if not prerequisites_checked:
            _run_with_prerequisites(runtime.prerequisite_checker)
            prerequisites_checked = True
        if effective_limit < item.limit:
            typer.echo(
                f"Pair {index}/{total_pairs}: clamped requested limit from {item.limit} to {effective_limit}."
            )

        try:
            outcome = runtime.service.scrape(
                item.business,
                item.location,
                effective_limit,
                now,
                _progress_renderer(),
                campaign_slug=item.campaign_slug,
                query=item.query,
                language=item.language,
                refresh_enrichment=refresh_enrichment,
            )
            launched_count += 1
        except RequestedLimitError as exc:
            typer.echo(f"Batch stopped at pair {index}/{total_pairs}: {_message_for_exception(exc)}")
            _render_batch_summary(completed_count, total_pairs)
            raise typer.Exit(code=2) from exc
        except (MapsLeadError, KeyError) as exc:
            typer.echo(f"Batch stopped at pair {index}/{total_pairs}: {_message_for_exception(exc)}")
            _render_batch_summary(completed_count, total_pairs)
            raise typer.Exit(code=1) from exc

        outcome_exit_code = _render_outcome_with_exit_code(outcome)
        if outcome.run.status is RunStatus.COMPLETED and outcome_exit_code == 0:
            completed_count += 1
        else:
            if outcome.run.status in {RunStatus.PARTIAL, RunStatus.BLOCKED, RunStatus.FAILED}:
                typer.echo(f"Resume: mapslead resume {outcome.run.id}")
            _render_batch_summary(completed_count, total_pairs)
            raise typer.Exit(code=outcome_exit_code or 1)

    assert launched_count == total_pairs
    _render_batch_summary(completed_count, total_pairs)


@app.command()
def quota(ctx: typer.Context) -> None:
    runtime = _runtime_for_context(ctx)
    now = runtime.clock.now()
    remaining = runtime.repository.remaining_quota(now)
    used = DAILY_NEW_RECORD_LIMIT - remaining
    typer.echo(f"Used today: {used}")
    typer.echo(f"Remaining: {remaining}")


@app.command()
def resume(ctx: typer.Context, run_id: str) -> None:
    runtime = _runtime_for_context(ctx)
    try:
        _ensure_resumable_run(runtime.repository, run_id)
        _run_with_prerequisites(runtime.prerequisite_checker)
        outcome = runtime.service.resume(run_id, runtime.clock.now(), _progress_renderer())
    except (MapsLeadError, KeyError) as exc:
        _exit_with_message(_message_for_exception(exc), code=1)
    _render_outcome(outcome)


@app.command()
def export(
    ctx: typer.Context,
    run_id: str = EXPORT_RUN_ID_OPTION,
) -> None:
    runtime = _runtime_for_context(ctx)
    try:
        runtime.repository.get_run(run_id)
        paths = runtime.exporter.export_run(run_id)
    except (MapsLeadError, KeyError) as exc:
        _exit_with_message(_message_for_exception(exc), code=1)
    _render_export_paths(paths)


@campaign_app.command("create")
def create_campaign(
    ctx: typer.Context,
    slug: str,
    business: str = BUSINESS_OPTION,
) -> None:
    runtime = _runtime_for_context(ctx)
    try:
        campaign = runtime.repository.create_campaign(slug, business, runtime.clock.now())
    except (MapsLeadError, KeyError) as exc:
        _exit_with_message(_message_for_exception(exc), code=1)
    typer.echo(f"Campaign: {campaign.slug}")
    typer.echo(f"Business type: {campaign.business_type}")


@campaign_app.command("attach-run")
def attach_campaign_run(
    ctx: typer.Context,
    slug: str,
    run_id: str,
) -> None:
    runtime = _runtime_for_context(ctx)
    try:
        campaign = runtime.repository.attach_run(slug, run_id, runtime.clock.now())
    except (MapsLeadError, KeyError) as exc:
        _exit_with_message(_message_for_exception(exc), code=1)
    typer.echo(f"Attached {run_id} to {campaign.slug}")


@campaign_app.command("status")
def campaign_status(ctx: typer.Context, slug: str) -> None:
    runtime = _runtime_for_context(ctx)
    try:
        status = runtime.repository.campaign_status(slug)
    except (MapsLeadError, KeyError) as exc:
        _exit_with_message(_message_for_exception(exc), code=1)
    _render_campaign_status(runtime, status)


@campaign_app.command("export")
def export_campaign(ctx: typer.Context, slug: str) -> None:
    runtime = _runtime_for_context(ctx)
    try:
        paths = runtime.campaign_exporter.export_campaign(slug)
    except (MapsLeadError, KeyError) as exc:
        _exit_with_message(_message_for_exception(exc), code=1)
    _render_export_paths(paths)


def _resolve_settings(*, data_dir: Path | None, export_dir: Path | None) -> Settings:
    base_settings = Settings.from_env()
    return Settings(
        data_dir=data_dir if data_dir is not None else base_settings.data_dir,
        export_dir=export_dir if export_dir is not None else base_settings.export_dir,
        timezone=base_settings.timezone,
    )


def _runtime_for_context(ctx: typer.Context) -> AppRuntime:
    settings = ctx.find_root().obj
    if not isinstance(settings, Settings):
        settings = _resolve_settings(data_dir=None, export_dir=None)
    return build_service(settings)


def _run_with_prerequisites(checker: Any) -> None:
    try:
        checker()
    except PrerequisiteError as exc:
        _exit_with_message(_message_for_exception(exc), code=1)


def _run_command(args: list[str]) -> CommandResult:
    completed = subprocess.run(
        args,
        check=False,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return CommandResult(args=args, returncode=completed.returncode)


def _ensure_resumable_run(repository: Any, run_id: str) -> None:
    run = repository.get_run(run_id)
    if run.status not in {RunStatus.PARTIAL, RunStatus.BLOCKED, RunStatus.FAILED}:
        raise ResumeNotAllowedError(f"run {run_id} cannot be resumed from status {run.status.value}")


def _progress_renderer() -> Any:
    def render(event: ProgressEvent) -> None:
        if event.kind == "acquisition":
            typer.echo(
                f"Acquired {event.candidate_count or 0} candidates ({event.new_unique_count or 0} new)."
            )
            return
        if event.kind == "enrichment":
            typer.echo(f"Enriched {event.completed_count or 0}/{event.total_count or 0} websites.")
            return
        if event.kind == "enrichment_reused":
            typer.echo(
                f"Reused cached enrichment {event.completed_count or 0}/{event.total_count or 0}."
            )
            return
        if event.kind == "export" and event.export_paths is not None:
            _render_export_paths(event.export_paths)

    return render


def _build_scrape_plan(
    *,
    business: str,
    campaign_slug: str | None,
    queries: Sequence[str] | None,
    locations: Sequence[str],
    language: str,
    limit: int,
) -> list[ScrapePlanItem]:
    validated_language = validate_language_code(language)
    validated_locations = [validate_location_query(value) for value in locations]
    validated_queries = [validate_search_query(value) for value in queries or ()]
    resolved_queries = [None] if not validated_queries else validated_queries
    return [
        ScrapePlanItem(
            business=business,
            location=resolved_location,
            limit=limit,
            query=resolved_query,
            language=validated_language,
            campaign_slug=campaign_slug,
        )
        for resolved_location in validated_locations
        for resolved_query in resolved_queries
    ]


def _render_outcome(outcome: Any) -> None:
    outcome_exit_code = _render_outcome_with_exit_code(outcome)
    if outcome_exit_code:
        raise typer.Exit(code=1)


def _render_outcome_with_exit_code(outcome: Any) -> int:
    typer.echo(f"Run {outcome.run.status.value}: {outcome.run.id}")
    if outcome.run.error:
        typer.echo(outcome.run.error)
    if outcome.service_error:
        typer.echo(outcome.service_error)
        return 1
    if outcome.export_paths is not None:
        _render_export_paths(outcome.export_paths)
    return 0


def _render_batch_summary(completed_count: int, total_pairs: int) -> None:
    typer.echo(f"Batch summary: {completed_count}/{total_pairs} runs completed.")


def _render_export_paths(paths: ExportPaths) -> None:
    typer.echo(f"CSV: {paths.csv_path}")
    typer.echo(f"JSON: {paths.json_path}")
    typer.echo(f"Excel: {paths.xlsx_path}")


def _render_campaign_status(runtime: AppRuntime, status: CampaignStatus) -> None:
    remaining = runtime.repository.remaining_quota(runtime.clock.now())
    used = DAILY_NEW_RECORD_LIMIT - remaining
    typer.echo(f"Campaign: {status.campaign.slug}")
    typer.echo(f"Business type: {status.campaign.business_type}")
    typer.echo(f"Runs: {status.run_count}")
    typer.echo(f"Businesses: {status.business_count}")
    typer.echo(f"Locations: {', '.join(status.discovered_in) if status.discovered_in else '-'}")
    typer.echo(
        "Enrichment: "
        f"{status.completed_count} completed, "
        f"{status.failed_count} failed, "
        f"{status.skipped_count} skipped, "
        f"{status.pending_count} pending"
    )
    typer.echo(f"Used today: {used}")
    typer.echo(f"Remaining: {remaining}")
    csv_path = runtime.settings.export_dir / "campaigns" / status.campaign.slug / "results.csv"
    json_path = runtime.settings.export_dir / "campaigns" / status.campaign.slug / "results.json"
    xlsx_path = runtime.settings.export_dir / "campaigns" / status.campaign.slug / "results.xlsx"
    if csv_path.exists() and json_path.exists() and xlsx_path.exists():
        _render_export_paths(ExportPaths(csv_path=csv_path, json_path=json_path, xlsx_path=xlsx_path))


def _message_for_exception(error: BaseException) -> str:
    if isinstance(error, KeyError) and len(error.args) == 1:
        return str(error.args[0])
    return str(error)


def _exit_with_message(message: str, *, code: int) -> None:
    typer.echo(message)
    raise typer.Exit(code=code)
