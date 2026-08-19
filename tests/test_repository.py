from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mapslead.config import DAILY_NEW_RECORD_LIMIT, Settings
from mapslead.errors import QuotaExceededError
from mapslead.models import EnrichmentResult, EnrichmentStatus, ProviderCandidate, RunStatus
from mapslead.repository import SQLiteRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")


@pytest.fixture
def repository(settings: Settings) -> SQLiteRepository:
    repo = SQLiteRepository(settings)
    repo.initialize()
    return repo


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
def candidate() -> ProviderCandidate:
    return ProviderCandidate(
        name="Example Dental",
        place_id="ChIJ-123",
        address="1 Main St",
        phone="+84 28 123 456",
        website="https://example.com",
        category="Dentist",
    )


def test_only_new_business_consumes_daily_quota(
    repository: SQLiteRepository,
    candidate: ProviderCandidate,
    now: datetime,
) -> None:
    run_a = repository.create_run("dentists", "HCMC", 10, now)
    first = repository.accept_candidate(run_a.id, candidate, now)
    second = repository.accept_candidate(run_a.id, candidate, now)

    assert first.is_new is True
    assert second.is_new is False
    assert repository.remaining_quota(now) == DAILY_NEW_RECORD_LIMIT - 1
    assert repository.new_unique_count_for_run(run_a.id) == 1
    assert len(repository.snapshots_for_run(run_a.id)) == 1


def test_fallback_alias_matches_later_candidate_with_place_id(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 10, now)
    first = ProviderCandidate(name="Example", address="1 Main St", phone="+84123")
    later = ProviderCandidate(name="Example", address="1 Main St", place_id="ChIJ-new")

    assert repository.accept_candidate(run.id, first, now).is_new is True
    later_acceptance = repository.accept_candidate(run.id, later, now)

    assert later_acceptance.is_new is False
    assert later_acceptance.identity.primary_key == "place:ChIJ-new"
    assert len(repository.snapshots_for_run(run.id)) == 1


def test_old_run_snapshot_is_immutable_after_later_sighting(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    first_run = repository.create_run("dentists", "HCMC", 10, now)
    original = ProviderCandidate(name="Example", place_id="ChIJ-1", phone="111")
    repository.accept_candidate(first_run.id, original, now)

    second_run = repository.create_run("dentists", "HCMC", 10, now)
    changed = ProviderCandidate(name="Example", place_id="ChIJ-1", phone="222")
    repository.accept_candidate(second_run.id, changed, now)

    assert repository.snapshots_for_run(first_run.id)[0].phone == "111"
    assert repository.snapshots_for_run(second_run.id)[0].phone == "222"


def test_quota_boundary_uses_asia_ho_chi_minh_day(
    repository: SQLiteRepository,
) -> None:
    before_midnight_utc = datetime(2026, 8, 19, 16, 59, tzinfo=UTC)
    after_midnight_utc = datetime(2026, 8, 19, 17, 1, tzinfo=UTC)
    run_before = repository.create_run("dentists", "HCMC", 10, before_midnight_utc)
    run_after = repository.create_run("dentists", "HCMC", 10, after_midnight_utc)

    repository.accept_candidate(
        run_before.id,
        ProviderCandidate(name="Example A", place_id="ChIJ-a"),
        before_midnight_utc,
    )
    repository.accept_candidate(
        run_after.id,
        ProviderCandidate(name="Example B", place_id="ChIJ-b"),
        after_midnight_utc,
    )

    assert repository.remaining_quota(before_midnight_utc) == DAILY_NEW_RECORD_LIMIT - 1
    assert repository.remaining_quota(after_midnight_utc) == DAILY_NEW_RECORD_LIMIT - 1


def test_rejected_candidate_does_not_consume_quota(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 10, now)

    with pytest.raises(ValueError, match="identity"):
        repository.accept_candidate(run.id, ProviderCandidate(name="Example"), now)

    assert repository.remaining_quota(now) == DAILY_NEW_RECORD_LIMIT
    assert repository.new_unique_count_for_run(run.id) == 0
    assert repository.snapshots_for_run(run.id) == ()


def test_duplicate_association_is_unique_per_run_and_persists_one_snapshot(
    repository: SQLiteRepository,
    candidate: ProviderCandidate,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 10, now)

    first = repository.accept_candidate(run.id, candidate, now)
    second = repository.accept_candidate(run.id, candidate, now)

    assert first.business_id == second.business_id
    assert len(repository.snapshots_for_run(run.id)) == 1


def test_reused_business_is_pending_enrichment_for_new_run(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    candidate = ProviderCandidate(
        name="Example Dental",
        place_id="ChIJ-123",
        website="https://example.com",
    )
    first_run = repository.create_run("dentists", "HCMC", 10, now)
    repository.accept_candidate(first_run.id, candidate, now)
    repository.save_enrichment(
        first_run.id,
        repository.snapshots_for_run(first_run.id)[0].business_id,
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("hello@example.com",),
        ),
        now,
    )

    second_run = repository.create_run("dentists", "HCMC", 10, now)
    acceptance = repository.accept_candidate(second_run.id, candidate, now)
    pending = repository.pending_enrichment(second_run.id)

    assert acceptance.is_new is False
    assert [snapshot.business_id for snapshot in pending] == [acceptance.business_id]
    assert pending[0].enrichment_status == EnrichmentStatus.PENDING


def test_pending_enrichment_uses_canonical_website_learned_later_in_same_run(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 10, now)
    first = repository.accept_candidate(
        run.id,
        ProviderCandidate(name="Example Dental", place_id="ChIJ-123"),
        now,
    )
    assert repository.pending_enrichment(run.id) == ()

    repository.accept_candidate(
        run.id,
        ProviderCandidate(
            name="Example Dental",
            place_id="ChIJ-123",
            website="https://example.com",
        ),
        now,
    )

    pending = repository.pending_enrichment(run.id)
    snapshot = repository.snapshots_for_run(run.id)[0]

    assert [item.business_id for item in pending] == [first.business_id]
    assert pending[0].website == "https://example.com"
    assert snapshot.website is None


def test_pending_enrichment_uses_canonical_website_for_later_run_reuse_without_mutating_older_snapshot(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    first_run = repository.create_run("dentists", "HCMC", 10, now)
    repository.accept_candidate(
        first_run.id,
        ProviderCandidate(
            name="Example Dental",
            place_id="ChIJ-123",
            website="https://example.com",
        ),
        now,
    )

    second_run = repository.create_run("dentists", "HCMC", 10, now)
    acceptance = repository.accept_candidate(
        second_run.id,
        ProviderCandidate(name="Example Dental", place_id="ChIJ-123"),
        now,
    )

    pending = repository.pending_enrichment(second_run.id)
    first_snapshot = repository.snapshots_for_run(first_run.id)[0]
    second_snapshot = repository.snapshots_for_run(second_run.id)[0]

    assert acceptance.is_new is False
    assert [item.business_id for item in pending] == [acceptance.business_id]
    assert pending[0].website == "https://example.com"
    assert first_snapshot.website == "https://example.com"
    assert second_snapshot.website is None


def test_save_enrichment_marks_snapshot_complete_for_run(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 10, now)
    acceptance = repository.accept_candidate(
        run.id,
        ProviderCandidate(
            name="Example Dental",
            place_id="ChIJ-123",
            website="https://example.com",
        ),
        now,
    )

    repository.save_enrichment(
        run.id,
        acceptance.business_id,
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("a@example.com", "b@example.com"),
            linkedin_url="https://linkedin.com/company/example",
        ),
        now,
    )

    assert repository.pending_enrichment(run.id) == ()
    snapshot = repository.snapshots_for_run(run.id)[0]
    assert snapshot.emails == ("a@example.com", "b@example.com")
    assert snapshot.linkedin_url == "https://linkedin.com/company/example"
    assert snapshot.enrichment_status == EnrichmentStatus.COMPLETED


def test_new_unique_count_only_tracks_new_insertions_and_survives_reopen(
    settings: Settings,
    now: datetime,
) -> None:
    first_repo = SQLiteRepository(settings)
    first_repo.initialize()
    first_run = first_repo.create_run("dentists", "HCMC", 10, now)
    second_run = first_repo.create_run("dentists", "HCMC", 10, now)
    candidate = ProviderCandidate(name="Example Dental", place_id="ChIJ-123")

    assert first_repo.accept_candidate(first_run.id, candidate, now).is_new is True
    assert first_repo.accept_candidate(first_run.id, candidate, now).is_new is False
    assert first_repo.accept_candidate(second_run.id, candidate, now).is_new is False
    assert first_repo.new_unique_count_for_run(first_run.id) == 1
    assert first_repo.new_unique_count_for_run(second_run.id) == 0

    reopened = SQLiteRepository(settings)
    assert reopened.new_unique_count_for_run(first_run.id) == 1
    assert reopened.new_unique_count_for_run(second_run.id) == 0


def test_set_run_status_updates_finished_at_and_error(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "HCMC", 10, now)
    finished_at = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)

    updated = repository.set_run_status(
        run.id,
        RunStatus.PARTIAL,
        finished_at=finished_at,
        error="provider interrupted",
    )

    assert updated.status is RunStatus.PARTIAL
    assert updated.finished_at == finished_at
    assert updated.error == "provider interrupted"
    assert repository.get_run(run.id) == updated


def test_two_connections_race_for_final_quota_slot(
    settings: Settings,
    now: datetime,
) -> None:
    seed_repo = SQLiteRepository(settings)
    seed_repo.initialize()
    seed_run = seed_repo.create_run("dentists", "HCMC", DAILY_NEW_RECORD_LIMIT, now)
    for index in range(DAILY_NEW_RECORD_LIMIT - 1):
        acceptance = seed_repo.accept_candidate(
            seed_run.id,
            ProviderCandidate(name=f"Seed {index}", place_id=f"seed-{index}"),
            now,
        )
        assert acceptance.is_new is True

    run_a = seed_repo.create_run("dentists", "HCMC", 10, now)
    run_b = seed_repo.create_run("dentists", "HCMC", 10, now)
    repo_a = SQLiteRepository(settings)
    repo_b = SQLiteRepository(settings)

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[QuotaExceededError | threading.BrokenBarrierError] = []
    lock = threading.Lock()

    def attempt(repo: SQLiteRepository, run_id: str, suffix: str) -> None:
        try:
            barrier.wait(timeout=5)
            accepted = repo.accept_candidate(
                run_id,
                ProviderCandidate(name=f"Race {suffix}", place_id=f"race-{suffix}"),
                now,
            )
        except (QuotaExceededError, threading.BrokenBarrierError) as exc:  # pragma: no cover
            with lock:
                errors.append(exc)
            return

        with lock:
            results.append("accepted" if accepted.is_new else "duplicate")

    thread_a = threading.Thread(target=attempt, args=(repo_a, run_a.id, "a"))
    thread_b = threading.Thread(target=attempt, args=(repo_b, run_b.id, "b"))
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()

    quota_errors = [error for error in errors if isinstance(error, QuotaExceededError)]

    assert len(results) == 1
    assert results == ["accepted"]
    assert len(quota_errors) == 1
    assert repository_remaining(settings, now) == 0


def repository_remaining(settings: Settings, now: datetime) -> int:
    reopened = SQLiteRepository(settings)
    return reopened.remaining_quota(now)


def test_initialize_creates_schema_version_one(settings: Settings) -> None:
    repository = SQLiteRepository(settings)
    repository.initialize()

    with sqlite3.connect(settings.data_dir / "mapslead.sqlite3") as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()

    assert version == (1,)
