from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mapslead import cli
from mapslead.config import DAILY_NEW_RECORD_LIMIT, DEFAULT_RUN_LIMIT, Settings
from mapslead.enrichment import FetchedPage, UrlPolicy, WebsiteEnrichmentService
from mapslead.exporter import Exporter
from mapslead.models import (
    EnrichmentStatus,
    ExportPaths,
    ProgressEvent,
    ProviderCandidate,
    ProviderRequest,
    ProviderResult,
    RunRecord,
    RunStatus,
)
from mapslead.repository import SQLiteRepository
from mapslead.service import MapsLeadService, RequestedLimitError, RunOutcome

runner = CliRunner()


@dataclass(slots=True)
class FakeClock:
    current: datetime = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return 0.0


@dataclass(slots=True)
class FakeRepository:
    remaining_quota_value: int = DAILY_NEW_RECORD_LIMIT
    remaining_quota_calls: list[datetime] = field(default_factory=list)
    runs_by_id: dict[str, RunRecord] = field(default_factory=dict)

    def initialize(self) -> None:
        return None

    def remaining_quota(self, now: datetime) -> int:
        self.remaining_quota_calls.append(now)
        return self.remaining_quota_value

    def get_run(self, run_id: str) -> RunRecord:
        if run_id not in self.runs_by_id:
            raise KeyError(f"run {run_id} not found")
        return self.runs_by_id[run_id]


@dataclass(slots=True)
class FakeExporter:
    paths: ExportPaths
    calls: list[str] = field(default_factory=list)

    def export_run(self, run_id: str) -> ExportPaths:
        self.calls.append(run_id)
        return self.paths


@dataclass(slots=True)
class FakeService:
    outcome: RunOutcome
    progress_events: tuple[ProgressEvent, ...] = ()
    scrape_error: Exception | None = None
    resume_error: Exception | None = None
    scrape_call: tuple[str, str, int] | None = None
    resume_call: str | None = None

    def scrape(
        self,
        business: str,
        location: str,
        limit: int,
        now: datetime,
        progress: Callable[[ProgressEvent], None],
    ) -> RunOutcome:
        self.scrape_call = (business, location, limit)
        for event in self.progress_events:
            progress(event)
        if self.scrape_error is not None:
            raise self.scrape_error
        return self.outcome

    def resume(self, run_id: str, now: datetime, progress: Callable[[ProgressEvent], None]) -> RunOutcome:
        self.resume_call = run_id
        for event in self.progress_events:
            progress(event)
        if self.resume_error is not None:
            raise self.resume_error
        return self.outcome


@dataclass(slots=True)
class FakeRuntime:
    settings: Settings
    repository: Any
    service: Any
    exporter: Any
    clock: FakeClock
    prerequisite_checker: Callable[[], None] = lambda: None


@dataclass(slots=True)
class CountingChecker:
    calls: int = 0
    error: Exception | None = None

    def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class TrackingRepository(SQLiteRepository):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.create_run_calls = 0

    def create_run(
        self,
        business: str,
        location: str,
        requested_limit: int,
        now: datetime,
    ) -> RunRecord:
        self.create_run_calls += 1
        return super().create_run(business, location, requested_limit, now)


@dataclass(frozen=True, slots=True)
class ProviderScript:
    candidates: tuple[ProviderCandidate, ...]
    result: ProviderResult


@dataclass(slots=True)
class FakeProvider:
    scripts: list[ProviderScript] = field(default_factory=list)
    acquire_requests: list[ProviderRequest] = field(default_factory=list)
    replay_requests: list[ProviderRequest] = field(default_factory=list)

    def acquire(self, request: ProviderRequest, sink: Any) -> ProviderResult:
        self.acquire_requests.append(request)
        script = self.scripts.pop(0)
        for candidate in script.candidates:
            sink(candidate)
        return script.result

    def replay(self, request: ProviderRequest, sink: Any) -> ProviderResult:
        self.replay_requests.append(request)
        for candidate in ():
            sink(candidate)
        return ProviderResult(
            status="completed",
            candidate_count=0,
            rejected_row_count=0,
            diagnostics_tail="",
        )


@dataclass(slots=True)
class FixturePageFetcher:
    pages: dict[str, FetchedPage]
    requested_urls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> FetchedPage:
        self.requested_urls.append(url)
        return self.pages[url]


@dataclass(slots=True)
class AllowAllRobots:
    def allows(self, url: str) -> tuple[bool, str | None]:
        return True, None


def _run_record(run_id: str, status: RunStatus = RunStatus.COMPLETED) -> RunRecord:
    return RunRecord(
        id=run_id,
        business_type="dentists",
        location_query="HCMC",
        requested_limit=25,
        status=status,
        started_at=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 19, 9, 5, tzinfo=UTC),
        provider_dir=Path("/tmp/provider"),
        error=None,
        new_unique_count=1,
    )


def _export_paths(run_id: str) -> ExportPaths:
    return ExportPaths(
        csv_path=Path(f"/tmp/exports/{run_id}/results.csv"),
        json_path=Path(f"/tmp/exports/{run_id}/results.json"),
    )


def _outcome(run_id: str, status: RunStatus = RunStatus.COMPLETED) -> RunOutcome:
    return RunOutcome(
        run=_run_record(run_id, status=status),
        provider_result=ProviderResult(
            status=status.value if status is not RunStatus.RUNNING else "completed",
            candidate_count=2,
            rejected_row_count=0,
            diagnostics_tail="done",
            interrupted=status is RunStatus.PARTIAL,
        ),
        export_paths=_export_paths(run_id),
        service_error=None,
    )


def _runtime(tmp_path: Path, *, service: Any, repository: Any | None = None, exporter: Any | None = None) -> FakeRuntime:
    settings = Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")
    active_repository = repository or FakeRepository(runs_by_id={"run-123": _run_record("run-123")})
    active_exporter = exporter or FakeExporter(_export_paths("run-123"))
    return FakeRuntime(
        settings=settings,
        repository=active_repository,
        service=service,
        exporter=active_exporter,
        clock=FakeClock(),
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, runtime: FakeRuntime) -> list[Settings]:
    captured_settings: list[Settings] = []

    def fake_build_service(settings: Settings) -> FakeRuntime:
        captured_settings.append(settings)
        return runtime

    monkeypatch.setattr(cli, "build_service", fake_build_service)
    return captured_settings


def test_scrape_command_passes_business_location_and_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path, service=FakeService(outcome=_outcome("run-123")))
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(
        cli.app,
        ["scrape", "--business", "dentists", "--location", "HCMC", "--limit", "25"],
    )

    assert result.exit_code == 0
    assert "results.csv" in result.stdout
    assert runtime.service.scrape_call == ("dentists", "HCMC", 25)


def test_scrape_command_uses_default_limit_when_not_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, service=FakeService(outcome=_outcome("run-123")))
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["scrape", "--business", "dentists", "--location", "HCMC"])

    assert result.exit_code == 0
    assert runtime.service.scrape_call == ("dentists", "HCMC", DEFAULT_RUN_LIMIT)


def test_quota_command_reports_used_and_remaining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository(remaining_quota_value=875)
    runtime = _runtime(
        tmp_path,
        repository=repository,
        service=FakeService(outcome=_outcome("run-123")),
    )
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["quota"])

    assert result.exit_code == 0
    assert "Used today: 125" in result.stdout
    assert "Remaining: 875" in result.stdout


def test_resume_command_uses_run_id_and_reports_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(runs_by_id={"run-456": _run_record("run-456", status=RunStatus.PARTIAL)})
    runtime = _runtime(
        tmp_path,
        repository=repository,
        service=FakeService(outcome=_outcome("run-456")),
    )
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["resume", "run-456"])

    assert result.exit_code == 0
    assert runtime.service.resume_call == "run-456"
    assert "results.json" in result.stdout


def test_export_command_regenerates_exports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = FakeExporter(_export_paths("run-789"))
    repository = FakeRepository(runs_by_id={"run-789": _run_record("run-789")})
    runtime = _runtime(
        tmp_path,
        repository=repository,
        service=FakeService(outcome=_outcome("run-123")),
        exporter=exporter,
    )
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["export", "--run-id", "run-789"])

    assert result.exit_code == 0
    assert exporter.calls == ["run-789"]
    assert "results.csv" in result.stdout


@pytest.mark.parametrize(
    ("status", "expected_phrase"),
    [
        (RunStatus.PARTIAL, "Run partial"),
        (RunStatus.BLOCKED, "Run blocked"),
    ],
)
def test_scrape_reports_partial_and_blocked_runs_with_export_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
    expected_phrase: str,
) -> None:
    runtime = _runtime(tmp_path, service=FakeService(outcome=_outcome("run-123", status=status)))
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["scrape", "--business", "dentists", "--location", "HCMC"])

    assert result.exit_code == 0
    assert expected_phrase in result.stdout
    assert "results.csv" in result.stdout


def test_scrape_renders_progress_updates_and_final_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(
        outcome=_outcome("run-123"),
        progress_events=(
            ProgressEvent(
                kind="acquisition",
                message="Accepted acquisition candidate.",
                run_id="run-123",
                candidate_count=2,
                new_unique_count=1,
            ),
            ProgressEvent(
                kind="enrichment",
                message="Saved enrichment checkpoint.",
                run_id="run-123",
                completed_count=1,
                total_count=1,
            ),
            ProgressEvent(
                kind="export",
                message="Exported run results.",
                run_id="run-123",
                export_paths=_export_paths("run-123"),
            ),
        ),
    )
    runtime = _runtime(tmp_path, service=service)
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["scrape", "--business", "dentists", "--location", "HCMC"])

    assert result.exit_code == 0
    assert "Acquired 2 candidates (1 new)." in result.stdout
    assert "Enriched 1/1 websites." in result.stdout
    assert "/tmp/exports/run-123/results.csv" in result.stdout


def test_scrape_returns_exit_code_two_for_quota_exhaustion_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(
        outcome=_outcome("run-123"),
        scrape_error=RequestedLimitError(
            "requested limit 25 must be between 1 and remaining daily quota 0"
        ),
    )
    runtime = _runtime(tmp_path, service=service)
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(
        cli.app,
        ["scrape", "--business", "dentists", "--location", "HCMC", "--limit", "25"],
    )

    assert result.exit_code == 2
    assert "remaining daily quota 0" in result.stdout
    assert "Traceback" not in result.stdout


def test_scrape_invalid_argument_returns_usage_exit_code() -> None:
    result = runner.invoke(
        cli.app,
        ["scrape", "--business", "dentists", "--location", "HCMC", "--limit", "invalid"],
    )

    assert result.exit_code == 2


def test_cli_data_and_export_dirs_override_environment_without_changing_timezone_or_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(remaining_quota_value=995)
    runtime = _runtime(
        tmp_path,
        repository=repository,
        service=FakeService(outcome=_outcome("run-123")),
    )
    captured_settings = _patch_runtime(monkeypatch, runtime)
    cli_data_dir = tmp_path / "cli-data"
    cli_export_dir = tmp_path / "cli-exports"
    monkeypatch.setenv("MAPSLEAD_DATA_DIR", str(tmp_path / "env-data"))
    monkeypatch.setenv("MAPSLEAD_EXPORT_DIR", str(tmp_path / "env-exports"))

    result = runner.invoke(
        cli.app,
        [
            "--data-dir",
            str(cli_data_dir),
            "--export-dir",
            str(cli_export_dir),
            "quota",
        ],
    )

    assert result.exit_code == 0
    assert captured_settings == [
        Settings(
            data_dir=cli_data_dir,
            export_dir=cli_export_dir,
            timezone=Settings.from_env().timezone,
        )
    ]
    assert "Used today: 5" in result.stdout
    assert "Remaining: 995" in result.stdout


def test_scrape_reports_prerequisite_guidance_without_creating_a_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")
    repository = TrackingRepository(settings)
    repository.initialize()
    exporter = Exporter(repository, settings)
    provider = FakeProvider(
        scripts=[
            ProviderScript(
                candidates=(),
                result=ProviderResult(
                    status="completed",
                    candidate_count=0,
                    rejected_row_count=0,
                    diagnostics_tail="",
                ),
            )
        ]
    )
    service = MapsLeadService(
        repository=repository,
        provider=provider,
        enricher=WebsiteEnrichmentService(
            page_fetcher=FixturePageFetcher({}),
            url_policy=UrlPolicy(resolver=_resolver()),
            robots_checker=AllowAllRobots(),
            clock=FakeClock(),
            sleeper=lambda _seconds: None,
        ),
        exporter=exporter,
    )
    runtime = FakeRuntime(
        settings=settings,
        repository=repository,
        service=service,
        exporter=exporter,
        clock=FakeClock(),
        prerequisite_checker=lambda: _raise_prerequisite("Docker is unavailable. Install and start Docker, then retry."),
    )
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["scrape", "--business", "dentists", "--location", "HCMC"])

    assert result.exit_code == 1
    assert "Docker is unavailable. Install and start Docker, then retry." in result.stdout
    assert repository.create_run_calls == 0


def test_prerequisite_checker_reports_docker_unavailable() -> None:
    checker = cli.OperatorPrerequisiteChecker(
        run_command=_missing_docker,
    )

    with pytest.raises(cli.PrerequisiteError, match="Docker is unavailable"):
        checker.check()


def test_prerequisite_checker_reports_missing_provider_image() -> None:
    checker = cli.OperatorPrerequisiteChecker(
        run_command=_missing_provider_image,
    )

    with pytest.raises(cli.PrerequisiteError, match="docker pull gosom/google-maps-scraper"):
        checker.check()

def test_prerequisite_checker_only_requires_docker_and_provider_image() -> None:
    checker = cli.OperatorPrerequisiteChecker(run_command=_docker_ok)

    checker.check()


def test_resume_missing_run_reports_accurate_error_before_prerequisite_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = CountingChecker()
    repository = FakeRepository(runs_by_id={})
    service = FakeService(outcome=_outcome("run-123"))
    runtime = _runtime(tmp_path, repository=repository, service=service)
    runtime.prerequisite_checker = checker
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["resume", "missing-run"])

    assert result.exit_code == 1
    assert "run missing-run not found" in result.stdout
    assert "Traceback" not in result.stdout
    assert checker.calls == 0
    assert service.resume_call is None


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.RUNNING])
def test_resume_non_resumable_status_reports_before_prerequisite_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
) -> None:
    checker = CountingChecker()
    run_id = f"run-{status.value}"
    repository = FakeRepository(runs_by_id={run_id: _run_record(run_id, status=status)})
    service = FakeService(outcome=_outcome("run-123"))
    runtime = _runtime(tmp_path, repository=repository, service=service)
    runtime.prerequisite_checker = checker
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["resume", run_id])

    assert result.exit_code == 1
    assert f"run {run_id} cannot be resumed from status {status.value}" in result.stdout
    assert "Traceback" not in result.stdout
    assert checker.calls == 0
    assert service.resume_call is None


def test_resume_resumable_run_checks_prerequisites_before_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = CountingChecker()
    run_id = "run-partial"
    repository = FakeRepository(runs_by_id={run_id: _run_record(run_id, status=RunStatus.PARTIAL)})
    service = FakeService(outcome=_outcome(run_id, status=RunStatus.PARTIAL))
    runtime = _runtime(tmp_path, repository=repository, service=service)
    runtime.prerequisite_checker = checker
    _patch_runtime(monkeypatch, runtime)

    result = runner.invoke(cli.app, ["resume", run_id])

    assert result.exit_code == 0
    assert checker.calls == 1
    assert service.resume_call == run_id


def test_offline_end_to_end_scrape_quota_and_reexport_are_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")
    repository = SQLiteRepository(settings)
    repository.initialize()
    exporter = Exporter(repository, settings)
    fixture_dir = Path(__file__).parent / "fixtures" / "web"
    home_html = (fixture_dir / "home.html").read_text(encoding="utf-8")
    contact_html = (fixture_dir / "contact.html").read_text(encoding="utf-8")
    fetcher = FixturePageFetcher(
        {
            "https://example.com": FetchedPage(final_url="https://example.com", html=home_html),
            "https://example.com/contact": FetchedPage(
                final_url="https://example.com/contact",
                html=contact_html,
            ),
            "https://example.com/about": FetchedPage(
                final_url="https://example.com/about",
                html="<p>About Example Dental</p>",
            ),
            "https://example.com/team": FetchedPage(
                final_url="https://example.com/team",
                html="<p>Meet the team</p>",
            ),
        }
    )
    provider = FakeProvider(
        scripts=[
            ProviderScript(
                candidates=(
                    ProviderCandidate(
                        place_id="place-1",
                        name="Example Dental",
                        category="Dentist",
                        address="1 Main St",
                        phone="+84 28 123 456",
                        website="https://example.com",
                        rating=4.8,
                        review_count=19,
                        google_maps_url="https://maps.google.com/?cid=place-1",
                    ),
                ),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    service = MapsLeadService(
        repository=repository,
        provider=provider,
        enricher=WebsiteEnrichmentService(
            page_fetcher=fetcher,
            url_policy=UrlPolicy(resolver=_resolver()),
            robots_checker=AllowAllRobots(),
            clock=FakeClock(),
            sleeper=lambda _seconds: None,
        ),
        exporter=exporter,
    )
    runtime = FakeRuntime(
        settings=settings,
        repository=repository,
        service=service,
        exporter=exporter,
        clock=FakeClock(),
    )
    _patch_runtime(monkeypatch, runtime)

    scrape_result = runner.invoke(
        cli.app,
        ["scrape", "--business", "dentists", "--location", "HCMC", "--limit", "1"],
    )

    assert scrape_result.exit_code == 0
    run_dir = next(settings.export_dir.iterdir())
    run_id = run_dir.name
    csv_path = run_dir / "results.csv"
    json_path = run_dir / "results.json"
    csv_rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))

    assert scrape_result.stdout.count("results.csv") >= 1
    assert fetcher.requested_urls == [
        "https://example.com",
        "https://example.com/contact",
        "https://example.com/about",
        "https://example.com/team",
    ]
    assert csv_rows == [
        {
            "place_id": "place-1",
            "name": "Example Dental",
            "category": "Dentist",
            "address": "1 Main St",
            "phone": "+84 28 123 456",
            "website": "https://example.com",
            "rating": "4.8",
            "review_count": "19",
            "google_maps_url": "https://maps.google.com/?cid=place-1",
            "emails": "hello@example.com;sales@example.com",
            "facebook_url": "https://www.facebook.com/ExampleDental",
            "instagram_url": "https://www.instagram.com/exampledental",
            "linkedin_url": "https://www.linkedin.com/company/example-dental",
            "x_url": "https://x.com/exampledental",
            "youtube_url": "https://www.youtube.com/@ExampleDental",
            "business_type": "dentists",
            "location_query": "HCMC",
            "first_seen_at": "2026-08-19T09:00:00+00:00",
            "last_seen_at": "2026-08-19T09:00:00+00:00",
            "enrichment_status": EnrichmentStatus.COMPLETED.value,
            "enrichment_error": "",
            "run_id": run_id,
        }
    ]
    assert json_rows == [
        {
            "place_id": "place-1",
            "name": "Example Dental",
            "category": "Dentist",
            "address": "1 Main St",
            "phone": "+84 28 123 456",
            "website": "https://example.com",
            "rating": 4.8,
            "review_count": 19,
            "google_maps_url": "https://maps.google.com/?cid=place-1",
            "emails": ["hello@example.com", "sales@example.com"],
            "facebook_url": "https://www.facebook.com/ExampleDental",
            "instagram_url": "https://www.instagram.com/exampledental",
            "linkedin_url": "https://www.linkedin.com/company/example-dental",
            "x_url": "https://x.com/exampledental",
            "youtube_url": "https://www.youtube.com/@ExampleDental",
            "business_type": "dentists",
            "location_query": "HCMC",
            "first_seen_at": "2026-08-19T09:00:00+00:00",
            "last_seen_at": "2026-08-19T09:00:00+00:00",
            "enrichment_status": EnrichmentStatus.COMPLETED.value,
            "enrichment_error": None,
            "run_id": run_id,
        }
    ]
    assert repository.get_run(run_id).status is RunStatus.COMPLETED
    assert repository.remaining_quota(FakeClock().now()) == DAILY_NEW_RECORD_LIMIT - 1

    initial_csv = csv_path.read_text(encoding="utf-8")
    initial_json = json_path.read_text(encoding="utf-8")
    export_result = runner.invoke(cli.app, ["export", "--run-id", run_id])
    quota_result = runner.invoke(cli.app, ["quota"])

    assert export_result.exit_code == 0
    assert quota_result.exit_code == 0
    assert "Used today: 1" in quota_result.stdout
    assert "Remaining: 999" in quota_result.stdout
    assert csv_path.read_text(encoding="utf-8") == initial_csv
    assert json_path.read_text(encoding="utf-8") == initial_json


def _resolver() -> Any:
    @dataclass(slots=True)
    class Resolver:
        def resolve(self, hostname: str) -> tuple[str, ...]:
            return {
                "example.com": ("93.184.216.34",),
            }[hostname]

    return Resolver()


def _raise_prerequisite(message: str) -> None:
    raise cli.PrerequisiteError(message)


def _docker_ok(args: list[str]) -> cli.CommandResult:
    return cli.CommandResult(args=args, returncode=0)


def _missing_docker(args: list[str]) -> cli.CommandResult:
    raise FileNotFoundError("docker")


def _missing_provider_image(args: list[str]) -> cli.CommandResult:
    if args[:2] == ["docker", "version"]:
        return cli.CommandResult(args=args, returncode=0)
    return cli.CommandResult(args=args, returncode=1)
