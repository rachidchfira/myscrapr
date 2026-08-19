from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mapslead.config import DAILY_NEW_RECORD_LIMIT, DEFAULT_RUN_LIMIT, Settings
from mapslead.errors import ExportError, QuotaExceededError
from mapslead.exporter import Exporter
from mapslead.models import (
    EnrichmentResult,
    EnrichmentStatus,
    ProgressEvent,
    ProviderCandidate,
    ProviderRequest,
    ProviderResult,
    RunStatus,
)
from mapslead.repository import SQLiteRepository
from mapslead.service import (
    MapsLeadService,
    RequestedLimitError,
    ResumeNotAllowedError,
    RunOutcome,
)


@dataclass(frozen=True, slots=True)
class ProviderScript:
    candidates: tuple[ProviderCandidate, ...] = ()
    result: ProviderResult = field(
        default_factory=lambda: ProviderResult(
            status="completed",
            candidate_count=0,
            rejected_row_count=0,
            diagnostics_tail="",
        )
    )


@dataclass(slots=True)
class FakeProvider:
    replay_scripts: list[ProviderScript] = field(default_factory=list)
    acquire_scripts: list[ProviderScript] = field(default_factory=list)
    replay_requests: list[ProviderRequest] = field(default_factory=list)
    acquire_requests: list[ProviderRequest] = field(default_factory=list)
    call_log: list[str] = field(default_factory=list)

    def replay(self, request: ProviderRequest, sink: Any) -> ProviderResult:
        self.call_log.append("replay")
        self.replay_requests.append(request)
        script = self.replay_scripts.pop(0) if self.replay_scripts else ProviderScript()
        for candidate in script.candidates:
            sink(candidate)
        return script.result

    def acquire(self, request: ProviderRequest, sink: Any) -> ProviderResult:
        self.call_log.append("acquire")
        self.acquire_requests.append(request)
        script = self.acquire_scripts.pop(0) if self.acquire_scripts else ProviderScript()
        for candidate in script.candidates:
            sink(candidate)
        return script.result


@dataclass(slots=True)
class FakeEnricher:
    responses: dict[str, EnrichmentResult | Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def enrich(self, website: str) -> EnrichmentResult:
        self.calls.append(website)
        response = self.responses.get(
            website,
            EnrichmentResult(status=EnrichmentStatus.COMPLETED),
        )
        if isinstance(response, Exception):
            raise response
        return response


@dataclass(slots=True)
class QuotaRaceRepository:
    inner: SQLiteRepository
    race_place_ids: set[str]
    raced_place_ids: set[str] = field(default_factory=set)
    delegated_place_ids: list[str | None] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def accept_candidate(self, run_id: str, candidate: ProviderCandidate, now: datetime) -> Any:
        if candidate.place_id in self.race_place_ids and candidate.place_id not in self.raced_place_ids:
            self.raced_place_ids.add(candidate.place_id)
            raise QuotaExceededError("daily quota exhausted during concurrent acceptance")
        self.delegated_place_ids.append(candidate.place_id)
        return self.inner.accept_candidate(run_id, candidate, now)


def _progress_sink(events: list[ProgressEvent]) -> Any:
    def sink(event: ProgressEvent) -> None:
        events.append(event)

    return sink


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")


@pytest.fixture
def repository(settings: Settings) -> SQLiteRepository:
    repo = SQLiteRepository(settings)
    repo.initialize()
    return repo


@pytest.fixture
def exporter(repository: SQLiteRepository, settings: Settings) -> Exporter:
    return Exporter(repository, settings)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


def test_scrape_rejects_invalid_limits_before_run_or_provider_call(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    provider = FakeProvider()
    enricher = FakeEnricher()
    service = MapsLeadService(repository, provider, enricher, exporter)
    progress_events: list[ProgressEvent] = []

    with pytest.raises(RequestedLimitError):
        service.scrape("dentists", "HCMC", 0, now, _progress_sink(progress_events))

    with pytest.raises(RequestedLimitError):
        service.scrape(
            "dentists",
            "HCMC",
            DAILY_NEW_RECORD_LIMIT + 1,
            now,
            _progress_sink(progress_events),
        )

    assert provider.acquire_requests == []
    assert count_runs(settings=repository._settings) == 0
    assert progress_events == []


def test_scrape_caps_new_unique_acceptance_at_default_limit_and_exports_results(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    candidates = tuple(
        candidate_for(index, website=f"https://biz-{index}.example.com")
        for index in range(DEFAULT_RUN_LIMIT + 5)
    )
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=candidates,
                result=ProviderResult(
                    status="completed",
                    candidate_count=len(candidates),
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    enricher = FakeEnricher()
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.scrape(
        "dentists",
        "HCMC",
        DEFAULT_RUN_LIMIT,
        now,
        _progress_sink([]),
    )

    assert_run_completed(outcome)
    assert provider.acquire_requests[0].max_new_records == DEFAULT_RUN_LIMIT
    assert repository.new_unique_count_for_run(outcome.run.id) == DEFAULT_RUN_LIMIT
    assert len(repository.snapshots_for_run(outcome.run.id)) == DEFAULT_RUN_LIMIT
    exported = load_export_json(outcome)
    assert len(exported) == DEFAULT_RUN_LIMIT
    assert exported[0]["run_id"] == outcome.run.id


def test_scrape_duplicate_candidates_do_not_consume_quota_twice_or_duplicate_export_rows(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    repeated = candidate_for(1, website="https://duplicate.example.com")
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(repeated, repeated),
                result=ProviderResult(
                    status="completed",
                    candidate_count=2,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    outcome = service.scrape("dentists", "HCMC", 10, now, _progress_sink([]))

    assert_run_completed(outcome)
    assert repository.remaining_quota(now) == DAILY_NEW_RECORD_LIMIT - 1
    assert repository.new_unique_count_for_run(outcome.run.id) == 1
    assert len(repository.snapshots_for_run(outcome.run.id)) == 1
    assert len(load_export_json(outcome)) == 1


def test_scrape_reuses_existing_business_and_enriches_current_run_without_checkpoint(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    seed_candidate = candidate_for(1, website="https://reused.example.com")
    first_run = repository.create_run("dentists", "HCMC", 5, now)
    first_acceptance = repository.accept_candidate(first_run.id, seed_candidate, now)
    repository.save_enrichment(
        first_run.id,
        first_acceptance.business_id,
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("first@example.com",),
        ),
        now,
    )
    repository.set_run_status(first_run.id, RunStatus.COMPLETED, finished_at=now)

    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(ProviderCandidate(name=seed_candidate.name, place_id=seed_candidate.place_id),),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://reused.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("second@example.com",),
            )
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink([]))

    assert_run_completed(outcome)
    assert repository.new_unique_count_for_run(outcome.run.id) == 0
    second_snapshot = repository.snapshots_for_run(outcome.run.id)[0]
    first_snapshot = repository.snapshots_for_run(first_run.id)[0]
    assert second_snapshot.emails == ("second@example.com",)
    assert first_snapshot.emails == ("first@example.com",)
    assert enricher.calls == ["https://reused.example.com"]


def test_scrape_campaign_reuses_cached_enrichment_without_fetch(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)
    seed_candidate = candidate_for(1, website="https://cached.example.com")
    seed_run = repository.create_run("dentists", "HCMC", 5, now)
    seed_acceptance = repository.accept_candidate(seed_run.id, seed_candidate, now)
    repository.save_cached_enrichment(
        seed_acceptance.business_id,
        "HTTPS://cached.example.com:443/#team",
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("cached@example.com",),
        ),
        now,
    )
    repository.set_run_status(seed_run.id, RunStatus.COMPLETED, finished_at=now)

    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(seed_candidate,),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    enricher = FakeEnricher()
    service = MapsLeadService(repository, provider, enricher, exporter)
    progress_events: list[ProgressEvent] = []

    outcome = service.scrape(
        "dentists",
        "Hanoi",
        5,
        now,
        _progress_sink(progress_events),
        campaign_slug=campaign.slug,
    )

    assert_run_completed(outcome)
    assert enricher.calls == []
    snapshot = repository.snapshots_for_run(outcome.run.id)[0]
    assert snapshot.emails == ("cached@example.com",)
    assert [event.kind for event in progress_events] == ["acquisition", "enrichment_reused", "export"]


def test_scrape_changed_website_invalidates_cache_and_updates_it(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    seed_candidate = candidate_for(1, website="https://old.example.com")
    seed_run = repository.create_run("dentists", "HCMC", 5, now)
    seed_acceptance = repository.accept_candidate(seed_run.id, seed_candidate, now)
    repository.save_cached_enrichment(
        seed_acceptance.business_id,
        seed_candidate.website or "",
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("old@example.com",),
        ),
        now,
    )
    repository.set_run_status(seed_run.id, RunStatus.COMPLETED, finished_at=now)

    updated_candidate = candidate_for(1, website="https://new.example.com")
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(updated_candidate,),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://new.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("new@example.com",),
            )
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.scrape("dentists", "Hanoi", 5, now, _progress_sink([]))

    assert_run_completed(outcome)
    assert enricher.calls == ["https://new.example.com"]
    snapshot = repository.snapshots_for_run(outcome.run.id)[0]
    assert snapshot.website == "https://new.example.com"
    assert snapshot.emails == ("new@example.com",)
    assert repository.cached_enrichment(seed_acceptance.business_id, "https://old.example.com") is None
    cached = repository.cached_enrichment(seed_acceptance.business_id, "https://new.example.com")
    assert cached is not None
    assert cached.result.emails == ("new@example.com",)


def test_scrape_refresh_enrichment_bypasses_matching_cache(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)
    seed_candidate = candidate_for(1, website="https://refresh.example.com")
    seed_run = repository.create_run("dentists", "HCMC", 5, now)
    seed_acceptance = repository.accept_candidate(seed_run.id, seed_candidate, now)
    repository.save_cached_enrichment(
        seed_acceptance.business_id,
        seed_candidate.website or "",
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("cached@example.com",),
        ),
        now,
    )
    repository.set_run_status(seed_run.id, RunStatus.COMPLETED, finished_at=now)

    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(seed_candidate,),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://refresh.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("refreshed@example.com",),
            )
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.scrape(
        "dentists",
        "Hanoi",
        5,
        now,
        _progress_sink([]),
        campaign_slug=campaign.slug,
        refresh_enrichment=True,
    )

    assert_run_completed(outcome)
    assert outcome.run.refresh_enrichment is True
    assert enricher.calls == ["https://refresh.example.com"]
    snapshot = repository.snapshots_for_run(outcome.run.id)[0]
    assert snapshot.emails == ("refreshed@example.com",)


def test_scrape_failed_enrichment_preserves_prior_successful_cache(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    seed_candidate = candidate_for(1, website="https://cache-failure.example.com")
    seed_run = repository.create_run("dentists", "HCMC", 5, now)
    seed_acceptance = repository.accept_candidate(seed_run.id, seed_candidate, now)
    repository.save_cached_enrichment(
        seed_acceptance.business_id,
        seed_candidate.website or "",
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("cached@example.com",),
        ),
        now,
    )
    repository.set_run_status(seed_run.id, RunStatus.COMPLETED, finished_at=now)

    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(seed_candidate,),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://cache-failure.example.com": RuntimeError("timeout contacting website")
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.scrape(
        "dentists",
        "Hanoi",
        5,
        now,
        _progress_sink([]),
        refresh_enrichment=True,
    )

    assert_run_completed(outcome)
    snapshot = repository.snapshots_for_run(outcome.run.id)[0]
    assert snapshot.enrichment_status is EnrichmentStatus.FAILED
    assert snapshot.emails == ()
    cached = repository.cached_enrichment(seed_acceptance.business_id, seed_candidate.website)
    assert cached is not None
    assert cached.result.emails == ("cached@example.com",)


def test_scrape_quota_race_stops_new_uniques_but_finishes_and_exports_known_duplicates(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    race_repository = QuotaRaceRepository(repository, race_place_ids={"place-2"})
    first = candidate_for(1, website="https://accepted.example.com")
    raced = candidate_for(2, website="https://raced.example.com")
    skipped = candidate_for(3, website="https://skipped.example.com")
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(first, raced, skipped, first),
                result=ProviderResult(
                    status="completed",
                    candidate_count=4,
                    rejected_row_count=0,
                    diagnostics_tail="provider completed",
                ),
            )
        ]
    )
    service = MapsLeadService(race_repository, provider, FakeEnricher(), exporter)
    progress_events: list[ProgressEvent] = []

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink(progress_events))

    assert outcome.run.status is RunStatus.COMPLETED
    assert outcome.run.error is None
    assert outcome.service_error is None
    assert outcome.export_paths is not None
    assert race_repository.delegated_place_ids == ["place-1", "place-1"]
    assert repository.get_run(outcome.run.id).status is RunStatus.COMPLETED
    assert repository.new_unique_count_for_run(outcome.run.id) == 1
    assert len(repository.snapshots_for_run(outcome.run.id)) == 1
    assert len(load_export_json(outcome)) == 1
    assert [event.kind for event in progress_events] == ["acquisition", "acquisition", "enrichment", "export"]


def test_scrape_persists_individual_enrichment_errors_without_failing_run(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    first = candidate_for(1, website="https://broken.example.com")
    second = candidate_for(2, website="https://ok.example.com")
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(first, second),
                result=ProviderResult(
                    status="completed",
                    candidate_count=2,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            )
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://broken.example.com": RuntimeError("timeout contacting website"),
            "https://ok.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("ok@example.com",),
            ),
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink([]))

    assert_run_completed(outcome)
    snapshots = repository.snapshots_for_run(outcome.run.id)
    assert snapshots[0].enrichment_status is EnrichmentStatus.FAILED
    assert snapshots[0].enrichment_error == "timeout contacting website"
    assert snapshots[1].emails == ("ok@example.com",)
    assert snapshots[1].enrichment_status is EnrichmentStatus.COMPLETED


@pytest.mark.parametrize("provider_status", [RunStatus.BLOCKED, RunStatus.FAILED])
def test_scrape_preserves_partial_records_for_blocked_and_failed_provider_results(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
    provider_status: RunStatus,
) -> None:
    candidate = candidate_for(1, website="https://status.example.com")
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(candidate,),
                result=ProviderResult(
                    status=provider_status.value,
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail=f"{provider_status.value} diagnostics",
                ),
            )
        ]
    )
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink([]))

    assert outcome.run.status is provider_status
    assert len(repository.snapshots_for_run(outcome.run.id)) == 1
    assert len(load_export_json(outcome)) == 1
    assert provider_status.value in (outcome.run.error or "")


def test_scrape_marks_keyboard_interruptions_partial_and_exports_records(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(candidate_for(1, website="https://partial.example.com"),),
                result=ProviderResult(
                    status="partial",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="interrupted by operator",
                    interrupted=True,
                ),
            )
        ]
    )
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink([]))

    assert outcome.run.status is RunStatus.PARTIAL
    assert outcome.run.error == "interrupted by operator"
    assert len(load_export_json(outcome)) == 1


def test_scrape_honors_partial_provider_status_without_interrupted_flag(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(candidate_for(1, website="https://partial-status.example.com"),),
                result=ProviderResult(
                    status="partial",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="provider reported partial completion",
                    interrupted=False,
                ),
            )
        ]
    )
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink([]))

    assert outcome.run.status is RunStatus.PARTIAL
    assert outcome.run.error == "provider reported partial completion"
    assert len(load_export_json(outcome)) == 1


def test_resume_replays_durable_rows_enriches_pending_only_and_finishes_same_run(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 3, now)
    first = repository.accept_candidate(run.id, candidate_for(1, website="https://pending.example.com"), now)
    second = repository.accept_candidate(
        run.id,
        candidate_for(2, website="https://done.example.com"),
        now,
    )
    repository.save_enrichment(
        run.id,
        second.business_id,
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("done@example.com",),
        ),
        now,
    )
    repository.set_run_status(run.id, RunStatus.PARTIAL, finished_at=now, error="previous partial")

    provider = FakeProvider(
        replay_scripts=[
            ProviderScript(
                candidates=(
                    candidate_for(1, website="https://pending.example.com"),
                    candidate_for(3, website="https://fresh.example.com"),
                ),
                result=ProviderResult(
                    status="completed",
                    candidate_count=2,
                    rejected_row_count=0,
                    diagnostics_tail="replayed",
                ),
            )
        ],
    )
    enricher = FakeEnricher(
        responses={
            "https://pending.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("pending@example.com",),
            ),
            "https://fresh.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("fresh@example.com",),
            ),
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.resume(run.id, now, _progress_sink([]))

    assert_run_completed(outcome)
    assert outcome.run.id == run.id
    assert provider.call_log == ["replay"]
    assert provider.acquire_requests == []
    assert enricher.calls == ["https://pending.example.com", "https://fresh.example.com"]
    snapshots = repository.snapshots_for_run(run.id)
    assert [snapshot.business_id for snapshot in snapshots] == [first.business_id, second.business_id, 3]
    assert repository.new_unique_count_for_run(run.id) == 3


def test_resume_rejects_running_and_completed_runs_before_provider_calls(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    running_run = repository.create_run("dentists", "HCMC", 1, now)
    previous_day = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
    completed_run = repository.create_run("dentists", "HCMC", 1, previous_day)
    repository.accept_candidate(completed_run.id, candidate_for(1), previous_day)
    repository.set_run_status(completed_run.id, RunStatus.COMPLETED, finished_at=previous_day)

    provider = FakeProvider()
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    with pytest.raises(ResumeNotAllowedError):
        service.resume(running_run.id, now, _progress_sink([]))

    with pytest.raises(ResumeNotAllowedError):
        service.resume(completed_run.id, now, _progress_sink([]))

    assert provider.call_log == []


def test_resume_refuses_completed_runs_and_limits_new_records_to_remaining_daily_quota(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:

    quota_seed_run = repository.create_run("dentists", "HCMC", DAILY_NEW_RECORD_LIMIT - 1, now)
    for index in range(2, DAILY_NEW_RECORD_LIMIT):
        repository.accept_candidate(quota_seed_run.id, candidate_for(index), now)
    limited_run = repository.create_run("dentists", "HCMC", 5, now)
    repository.accept_candidate(limited_run.id, candidate_for(10_001, website="https://seed.example.com"), now)
    repository.set_run_status(limited_run.id, RunStatus.PARTIAL, finished_at=now, error="resume me")

    provider = FakeProvider(
        replay_scripts=[
            ProviderScript(
                candidates=(candidate_for(10_001, website="https://seed.example.com"),),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="replayed",
                ),
            )
        ],
        acquire_scripts=[
            ProviderScript(
                candidates=(
                    candidate_for(10_002, website="https://only-one.example.com"),
                    candidate_for(10_003, website="https://ignored.example.com"),
                ),
                result=ProviderResult(
                    status="completed",
                    candidate_count=2,
                    rejected_row_count=0,
                    diagnostics_tail="quota limited",
                ),
            )
        ],
    )
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    outcome = service.resume(limited_run.id, now, _progress_sink([]))

    assert_run_completed(outcome)
    assert provider.acquire_requests[0].max_new_records == 1
    assert repository.new_unique_count_for_run(limited_run.id) == 2
    assert len(repository.snapshots_for_run(limited_run.id)) == 2
    assert repository.remaining_quota(now) == 0


def test_resume_at_requested_limit_replays_and_exports_without_new_acquisition(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 1, now)
    acceptance = repository.accept_candidate(
        run.id,
        candidate_for(1, website="https://resume-only.example.com"),
        now,
    )
    repository.set_run_status(run.id, RunStatus.PARTIAL, finished_at=now, error="resume me")

    provider = FakeProvider(
        replay_scripts=[
            ProviderScript(
                candidates=(candidate_for(1, website="https://resume-only.example.com"),),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="replayed",
                ),
            )
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://resume-only.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("resume@example.com",),
            )
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.resume(run.id, now, _progress_sink([]))

    assert_run_completed(outcome)
    assert outcome.run.id == run.id
    assert provider.call_log == ["replay"]
    assert provider.acquire_requests == []
    snapshot = repository.snapshots_for_run(run.id)[0]
    assert snapshot.business_id == acceptance.business_id
    assert snapshot.emails == ("resume@example.com",)


def test_resume_uses_persisted_refresh_enrichment_flag(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    seed_candidate = candidate_for(1, website="https://resume-refresh.example.com")
    seed_run = repository.create_run("dentists", "HCMC", 5, now)
    seed_acceptance = repository.accept_candidate(seed_run.id, seed_candidate, now)
    repository.save_cached_enrichment(
        seed_acceptance.business_id,
        seed_candidate.website or "",
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("cached@example.com",),
        ),
        now,
    )
    repository.set_run_status(seed_run.id, RunStatus.COMPLETED, finished_at=now)

    run = repository.create_run("dentists", "HCMC", 1, now, refresh_enrichment=True)
    repository.accept_candidate(run.id, seed_candidate, now)
    repository.set_run_status(run.id, RunStatus.PARTIAL, finished_at=now, error="resume me")

    provider = FakeProvider(
        replay_scripts=[
            ProviderScript(
                candidates=(seed_candidate,),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="replayed",
                ),
            )
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://resume-refresh.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("fresh@example.com",),
            )
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    outcome = service.resume(run.id, now, _progress_sink([]))

    assert_run_completed(outcome)
    assert enricher.calls == ["https://resume-refresh.example.com"]
    snapshot = repository.snapshots_for_run(run.id)[0]
    assert snapshot.emails == ("fresh@example.com",)


@pytest.mark.parametrize(
    ("replay_status", "diagnostics"),
    [
        ("failed", "replay failed after durable rows"),
        ("blocked", "replay detected blocking"),
        ("partial", "replay remained partial"),
    ],
)
def test_resume_non_completed_replay_preserves_status_and_skips_fresh_acquisition(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
    replay_status: str,
    diagnostics: str,
) -> None:
    run = repository.create_run("dentists", "HCMC", 3, now)
    repository.accept_candidate(
        run.id,
        candidate_for(1, website="https://replay-terminal.example.com"),
        now,
    )
    repository.set_run_status(run.id, RunStatus.PARTIAL, finished_at=now, error="resume me")

    provider = FakeProvider(
        replay_scripts=[
            ProviderScript(
                candidates=(candidate_for(1, website="https://replay-terminal.example.com"),),
                result=ProviderResult(
                    status=replay_status,
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail=diagnostics,
                    interrupted=False,
                ),
            )
        ],
        acquire_scripts=[
            ProviderScript(
                candidates=(candidate_for(2, website="https://should-not-run.example.com"),),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="unexpected acquire",
                ),
            )
        ],
    )
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    outcome = service.resume(run.id, now, _progress_sink([]))

    assert provider.call_log == ["replay"]
    assert provider.acquire_requests == []
    expected_status = RunStatus(replay_status)
    assert outcome.run.status is expected_status
    assert outcome.run.error == diagnostics
    assert outcome.export_paths is not None
    assert len(load_export_json(outcome)) == 1


def test_export_failure_keeps_records_durable_and_preserves_provider_status(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(candidate_for(1, website="https://blocked.example.com"),),
                result=ProviderResult(
                    status="blocked",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="captcha encountered",
                ),
            )
        ]
    )
    service = MapsLeadService(repository, provider, FakeEnricher(), exporter)

    def fail_export(run_id: str) -> Any:
        raise ExportError(f"failed to export run {run_id}")

    monkeypatch.setattr(exporter, "export_run", fail_export)

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink([]))

    assert outcome.run.status is RunStatus.BLOCKED
    assert outcome.run.error == "captcha encountered"
    assert outcome.service_error == f"failed to export run {outcome.run.id}"
    assert outcome.export_paths is None
    assert len(repository.snapshots_for_run(outcome.run.id)) == 1


def test_progress_events_report_counts_and_export_paths_without_secrets(
    repository: SQLiteRepository,
    exporter: Exporter,
    now: datetime,
) -> None:
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(
                    candidate_for(
                        1,
                        website="https://user:secret@progress.example.com/private",
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
    enricher = FakeEnricher(
        responses={
            "https://user:secret@progress.example.com/private": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("progress@example.com",),
            )
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)
    progress_events: list[ProgressEvent] = []

    outcome = service.scrape("dentists", "HCMC", 5, now, _progress_sink(progress_events))

    assert_run_completed(outcome)
    assert [event.kind for event in progress_events] == [
        "acquisition",
        "enrichment",
        "export",
    ]
    assert progress_events[0].candidate_count == 1
    assert progress_events[0].new_unique_count == 1
    assert progress_events[1].completed_count == 1
    assert progress_events[1].total_count == 1
    assert progress_events[2].export_paths == outcome.export_paths
    for event in progress_events:
        assert "secret" not in event.message
        assert "user:secret@" not in event.message
        assert "<html>" not in event.message


def candidate_for(
    index: int,
    *,
    website: str | None = None,
) -> ProviderCandidate:
    return ProviderCandidate(
        place_id=f"place-{index}",
        name=f"Business {index}",
        category="Dentist",
        address=f"{index} Main St",
        phone=f"+84 28 1000 {index:04d}",
        website=website,
        rating=4.5,
        review_count=index,
        google_maps_url=f"https://maps.example.com/{index}",
    )


def count_runs(*, settings: Settings) -> int:
    database_path = settings.data_dir / "mapslead.sqlite3"
    if not database_path.exists():
        return 0
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert row is not None
    return int(row[0])


def load_export_json(outcome: RunOutcome) -> list[dict[str, Any]]:
    assert outcome.export_paths is not None
    return json.loads(outcome.export_paths.json_path.read_text(encoding="utf-8"))


def assert_run_completed(outcome: RunOutcome) -> None:
    assert outcome.run.status is RunStatus.COMPLETED
    assert outcome.service_error is None
    assert outcome.export_paths is not None
