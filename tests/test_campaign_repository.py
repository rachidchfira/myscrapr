from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mapslead.config import DAILY_NEW_RECORD_LIMIT, Settings
from mapslead.errors import (
    CampaignBusinessTypeError,
    CampaignNotFoundError,
    CampaignRunAssignmentError,
    InvalidCampaignError,
)
from mapslead.models import (
    EnrichmentResult,
    EnrichmentStatus,
    ProviderCandidate,
    RunSnapshot,
    RunStatus,
)
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


@pytest.fixture
def existing_run_id(settings: Settings, now: datetime) -> str:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database_path = settings.data_dir / "mapslead.sqlite3"
    provider_dir = settings.data_dir / "runs" / "legacy-run" / "provider"
    provider_dir.mkdir(parents=True, exist_ok=True)
    snapshot = RunSnapshot(
        business_id=1,
        run_id="legacy-run",
        name="Legacy Dental",
        business_type="dentists",
        location_query="HCMC",
        first_seen_at=now,
        last_seen_at=now,
        place_id="legacy-place",
        category="Dentist",
        address="1 Legacy St",
        phone="+8428123456",
        website="https://legacy.example.com",
        enrichment_status=EnrichmentStatus.COMPLETED,
        emails=("hello@legacy.example.com",),
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version(version INTEGER NOT NULL);
            CREATE TABLE businesses(
                id INTEGER PRIMARY KEY,
                place_id TEXT UNIQUE,
                canonical_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE identity_aliases(
                alias TEXT PRIMARY KEY,
                business_id INTEGER NOT NULL REFERENCES businesses(id)
            );
            CREATE TABLE runs(
                id TEXT PRIMARY KEY,
                business_type TEXT NOT NULL,
                location_query TEXT NOT NULL,
                requested_limit INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                provider_dir TEXT NOT NULL,
                error TEXT,
                new_unique_count INTEGER NOT NULL DEFAULT 0 CHECK(new_unique_count >= 0)
            );
            CREATE TABLE run_businesses(
                run_id TEXT NOT NULL REFERENCES runs(id),
                business_id INTEGER NOT NULL REFERENCES businesses(id),
                snapshot_json TEXT NOT NULL,
                enrichment_status TEXT NOT NULL,
                enrichment_error TEXT,
                PRIMARY KEY(run_id, business_id)
            );
            CREATE TABLE daily_quota(
                day TEXT PRIMARY KEY,
                accepted_count INTEGER NOT NULL CHECK(accepted_count BETWEEN 0 AND 1000)
            );
            INSERT INTO schema_version(version) VALUES (1);
            INSERT INTO businesses(id, place_id, canonical_json, first_seen_at, last_seen_at)
            VALUES (
                1,
                'legacy-place',
                '{"place_id":"legacy-place","name":"Legacy Dental","category":"Dentist","address":"1 Legacy St","phone":"+8428123456","website":"https://legacy.example.com","emails":["hello@legacy.example.com"]}',
                '2026-08-19T10:00:00+00:00',
                '2026-08-19T10:00:00+00:00'
            );
            INSERT INTO identity_aliases(alias, business_id) VALUES ('place:legacy-place', 1);
            INSERT INTO daily_quota(day, accepted_count) VALUES ('2026-08-19', 1);
            """
        )
        connection.execute(
            """
            INSERT INTO runs(
                id,
                business_type,
                location_query,
                requested_limit,
                status,
                started_at,
                finished_at,
                provider_dir,
                error,
                new_unique_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-run",
                "dentists",
                "HCMC",
                10,
                "completed",
                "2026-08-19T10:00:00+00:00",
                "2026-08-19T10:05:00+00:00",
                str(provider_dir),
                None,
                1,
            ),
        )
        connection.execute(
            """
            INSERT INTO run_businesses(run_id, business_id, snapshot_json, enrichment_status, enrichment_error)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-run", 1, snapshot.model_dump_json(), "completed", None),
        )
    return "legacy-run"


@pytest.fixture
def v1_repository(settings: Settings, existing_run_id: str) -> SQLiteRepository:
    del existing_run_id
    return SQLiteRepository(settings)


@pytest.mark.parametrize("slug", ["Vietnam", "-vietnam", "vietnam-", "viet--nam", "viet_nam", "a" * 65])
def test_create_campaign_rejects_unsafe_slug(
    repository: SQLiteRepository,
    slug: str,
    now: datetime,
) -> None:
    with pytest.raises(InvalidCampaignError):
        repository.create_campaign(slug, "dentists", now)


def test_v1_database_migrates_without_losing_existing_run(
    v1_repository: SQLiteRepository,
    settings: Settings,
    existing_run_id: str,
    now: datetime,
) -> None:
    v1_repository.initialize()

    run = v1_repository.get_run(existing_run_id)
    snapshots = v1_repository.snapshots_for_run(existing_run_id)

    assert run.status is RunStatus.COMPLETED
    assert run.refresh_enrichment is False
    assert len(snapshots) == 1
    assert snapshots[0].emails == ("hello@legacy.example.com",)
    assert v1_repository.remaining_quota(now) == DAILY_NEW_RECORD_LIMIT - 1

    with sqlite3.connect(settings.data_dir / "mapslead.sqlite3") as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        refresh = connection.execute("SELECT refresh_enrichment FROM runs WHERE id = ?", (existing_run_id,)).fetchone()

    assert version == (2,)
    assert refresh == (0,)


def test_create_campaign_persists_and_gets_by_slug(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    created = repository.create_campaign("vietnam-dentists", "  Dentists  ", now)

    assert created.slug == "vietnam-dentists"
    assert created.business_type == "  Dentists  "
    assert created.created_at == now
    assert repository.get_campaign(created.slug) == created


def test_create_campaign_rejects_duplicate_slug(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    repository.create_campaign("vietnam-dentists", "dentists", now)

    with pytest.raises(InvalidCampaignError, match="already exists"):
        repository.create_campaign("vietnam-dentists", "dentists", now)


def test_attach_run_is_idempotent_for_same_campaign_and_preserves_quota(
    repository: SQLiteRepository,
    candidate: ProviderCandidate,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "Hanoi", 10, now)
    repository.accept_candidate(run.id, candidate, now)
    repository.accept_candidate(run.id, candidate, now)
    remaining_before_attach = repository.remaining_quota(now)
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)

    repository.attach_run(campaign.slug, run.id, now)
    repository.attach_run(campaign.slug, run.id, now)

    status = repository.campaign_status(campaign.slug)

    assert repository.campaign_for_run(run.id) == campaign
    assert status.run_count == 1
    assert status.business_count == 1
    assert status.discovered_in == ("Hanoi",)
    assert repository.remaining_quota(now) == remaining_before_attach


def test_attach_run_rejects_missing_run(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)

    with pytest.raises(KeyError, match="run missing-run not found"):
        repository.attach_run(campaign.slug, "missing-run", now)


def test_attach_run_rejects_missing_campaign(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "Hanoi", 10, now)

    with pytest.raises(CampaignNotFoundError, match="missing-campaign"):
        repository.attach_run("missing-campaign", run.id, now)


def test_attach_run_rejects_business_type_mismatch(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("Dentists", "Hanoi", 10, now)
    campaign = repository.create_campaign("vietnam-plumbers", " plumbers ", now)

    with pytest.raises(CampaignBusinessTypeError):
        repository.attach_run(campaign.slug, run.id, now)


def test_attach_run_rejects_assignment_to_another_campaign(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    run = repository.create_run("dentists", "Hanoi", 10, now)
    first = repository.create_campaign("vietnam-dentists", "dentists", now)
    second = repository.create_campaign("nationwide-dentists", "dentists", now)

    repository.attach_run(first.slug, run.id, now)

    with pytest.raises(CampaignRunAssignmentError):
        repository.attach_run(second.slug, run.id, now)


def test_create_run_with_campaign_assigns_membership_and_businesses_automatically(
    repository: SQLiteRepository,
    candidate: ProviderCandidate,
    now: datetime,
) -> None:
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)

    run = repository.create_run("dentists", "Da Nang", 10, now, campaign_slug=campaign.slug)
    repository.accept_candidate(run.id, candidate, now)

    status = repository.campaign_status(campaign.slug)
    snapshots = repository.campaign_snapshots(campaign.slug)

    assert repository.campaign_for_run(run.id) == campaign
    assert status.run_count == 1
    assert status.business_count == 1
    assert status.pending_count == 1
    assert status.completed_count == 0
    assert snapshots[0].campaign_id == campaign.slug
    assert snapshots[0].discovered_in == ("Da Nang",)


def test_create_run_with_campaign_rejects_business_type_mismatch(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)

    with pytest.raises(CampaignBusinessTypeError):
        repository.create_run("plumbers", "Hanoi", 10, now, campaign_slug=campaign.slug)


def test_campaign_snapshots_use_latest_campaign_snapshot_and_canonical_enrichment(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)
    first_run = repository.create_run("dentists", "HCMC", 10, now, campaign_slug=campaign.slug)
    first_acceptance = repository.accept_candidate(
        first_run.id,
        ProviderCandidate(
            name="Example Dental",
            place_id="ChIJ-123",
            website="https://example.com",
            phone="111",
        ),
        now,
    )
    repository.save_enrichment(
        first_run.id,
        first_acceptance.business_id,
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("hello@example.com",),
        ),
        now,
    )

    later = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    second_run = repository.create_run("dentists", "Hanoi", 10, later, campaign_slug=campaign.slug)
    repository.accept_candidate(
        second_run.id,
        ProviderCandidate(
            name="Example Dental",
            place_id="ChIJ-123",
            phone="222",
        ),
        later,
    )

    snapshots = repository.campaign_snapshots(campaign.slug)

    assert len(snapshots) == 1
    assert snapshots[0].campaign_id == campaign.slug
    assert snapshots[0].discovered_in == ("HCMC", "Hanoi")
    assert snapshots[0].phone == "222"
    assert snapshots[0].emails == ()
    assert snapshots[0].website == "https://example.com"
    assert snapshots[0].enrichment_status is EnrichmentStatus.PENDING


def test_campaign_status_counts_completed_failed_skipped_and_pending(
    repository: SQLiteRepository,
    now: datetime,
) -> None:
    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)

    completed_run = repository.create_run("dentists", "HCMC", 10, now, campaign_slug=campaign.slug)
    completed = repository.accept_candidate(
        completed_run.id,
        ProviderCandidate(name="Completed", place_id="place-completed", website="https://completed.example.com"),
        now,
    )
    repository.save_enrichment(
        completed_run.id,
        completed.business_id,
        EnrichmentResult(status=EnrichmentStatus.COMPLETED, emails=("completed@example.com",)),
        now,
    )

    failed_run = repository.create_run("dentists", "Hue", 10, now, campaign_slug=campaign.slug)
    failed = repository.accept_candidate(
        failed_run.id,
        ProviderCandidate(name="Failed", place_id="place-failed", website="https://failed.example.com"),
        now,
    )
    repository.save_enrichment(
        failed_run.id,
        failed.business_id,
        EnrichmentResult(status=EnrichmentStatus.FAILED, error="timeout"),
        now,
    )

    skipped_run = repository.create_run("dentists", "Da Nang", 10, now, campaign_slug=campaign.slug)
    repository.accept_candidate(
        skipped_run.id,
        ProviderCandidate(name="Skipped", place_id="place-skipped"),
        now,
    )

    pending_run = repository.create_run("dentists", "Can Tho", 10, now, campaign_slug=campaign.slug)
    repository.accept_candidate(
        pending_run.id,
        ProviderCandidate(name="Pending", place_id="place-pending", website="https://pending.example.com"),
        now,
    )

    status = repository.campaign_status(campaign.slug)

    assert status.run_count == 4
    assert status.business_count == 4
    assert status.discovered_in == ("Can Tho", "Da Nang", "HCMC", "Hue")
    assert status.completed_count == 1
    assert status.failed_count == 1
    assert status.skipped_count == 1
    assert status.pending_count == 1
