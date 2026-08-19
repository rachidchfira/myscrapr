from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from mapslead.config import DAILY_NEW_RECORD_LIMIT, Settings
from mapslead.errors import (
    CampaignBusinessTypeError,
    CampaignNotFoundError,
    CampaignRunAssignmentError,
    InvalidCampaignError,
    QuotaExceededError,
)
from mapslead.input_validation import validate_language_code, validate_search_query
from mapslead.models import (
    Acceptance,
    CampaignRecord,
    CampaignSnapshot,
    CampaignStatus,
    EnrichmentCacheEntry,
    EnrichmentResult,
    EnrichmentStatus,
    ProviderCandidate,
    RunRecord,
    RunSnapshot,
    RunStatus,
)
from mapslead.normalize import (
    build_identity,
    normalize_text,
    normalize_website_url,
    validate_campaign_slug,
)

_SCHEMA_VERSION = 3
_SCHEMA = """
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
    search_query TEXT NOT NULL,
    language TEXT NOT NULL,
    requested_limit INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    provider_dir TEXT NOT NULL,
    error TEXT,
    new_unique_count INTEGER NOT NULL DEFAULT 0 CHECK(new_unique_count >= 0),
    refresh_enrichment INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE campaigns(
    slug TEXT PRIMARY KEY,
    business_type TEXT NOT NULL,
    normalized_business_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE campaign_runs(
    campaign_slug TEXT NOT NULL REFERENCES campaigns(slug),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
    attached_at TEXT NOT NULL,
    PRIMARY KEY(campaign_slug, run_id)
);
CREATE TABLE campaign_businesses(
    campaign_slug TEXT NOT NULL REFERENCES campaigns(slug),
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    first_discovered_at TEXT NOT NULL,
    last_discovered_at TEXT NOT NULL,
    PRIMARY KEY(campaign_slug, business_id)
);
CREATE TABLE business_enrichment_cache(
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
    normalized_website TEXT NOT NULL,
    result_json TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
"""
_RUN_SELECT = """
SELECT
    runs.*,
    campaign_runs.campaign_slug AS campaign_slug
FROM runs
LEFT JOIN campaign_runs ON campaign_runs.run_id = runs.id
"""


class _CanonicalBusiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    place_id: str | None = None
    name: str | None = None
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    review_count: int | None = None
    google_maps_url: str | None = None
    emails: tuple[str, ...] = ()
    facebook_url: str | None = None
    instagram_url: str | None = None
    linkedin_url: str | None = None
    x_url: str | None = None
    youtube_url: str | None = None


class SQLiteRepository:
    def __init__(self, settings: Settings, *, busy_timeout_ms: int = 5_000) -> None:
        self._settings = settings
        self._busy_timeout_ms = busy_timeout_ms
        self._db_path = settings.data_dir / "mapslead.sqlite3"

    def initialize(self) -> None:
        self._settings.data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            schema_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
            ).fetchone()
            if schema_exists is None:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
                return

            row = connection.execute("SELECT version FROM schema_version").fetchone()
            version = int(_require_row(row, "schema_version")[0])
            if version == _SCHEMA_VERSION:
                return
            if version not in {1, 2}:
                raise RuntimeError(f"unsupported schema version: {version}")

            connection.execute("BEGIN IMMEDIATE")
            try:
                if version == 1:
                    self._migrate_v1_to_v2(connection)
                self._migrate_v2_to_v3(connection)
                connection.execute("UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,))
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def create_run(
        self,
        business: str,
        location: str,
        requested_limit: int,
        now: datetime,
        *,
        campaign_slug: str | None = None,
        query: str | None = None,
        language: str = "en",
        refresh_enrichment: bool = False,
    ) -> RunRecord:
        run_id = uuid4().hex
        provider_dir = self._settings.data_dir / "runs" / run_id / "provider"
        provider_dir.mkdir(parents=True, exist_ok=True)
        validated_campaign_slug = None if campaign_slug is None else validate_campaign_slug(campaign_slug)
        validated_query = validate_search_query(business if query is None else query)
        validated_language = validate_language_code(language)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                campaign_row = None
                if validated_campaign_slug is not None:
                    campaign_row = self._get_campaign_row(connection, validated_campaign_slug)
                    self._ensure_business_type_matches(
                        business_type=business,
                        normalized_campaign_business_type=str(campaign_row["normalized_business_type"]),
                    )

                connection.execute(
                    """
                    INSERT INTO runs(
                        id,
                        business_type,
                        location_query,
                        search_query,
                        language,
                        requested_limit,
                        status,
                        started_at,
                        finished_at,
                        provider_dir,
                        error,
                        new_unique_count,
                        refresh_enrichment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        business,
                        location,
                        validated_query,
                        validated_language,
                        requested_limit,
                        RunStatus.RUNNING.value,
                        now.isoformat(),
                        None,
                        str(provider_dir),
                        None,
                        0,
                        int(refresh_enrichment),
                    ),
                )
                if validated_campaign_slug is not None:
                    connection.execute(
                        """
                        INSERT INTO campaign_runs(campaign_slug, run_id, attached_at)
                        VALUES (?, ?, ?)
                        """,
                        (validated_campaign_slug, run_id, now.isoformat()),
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

        return self.get_run(run_id)

    def create_campaign(self, slug: str, business: str, now: datetime) -> CampaignRecord:
        validated_slug = validate_campaign_slug(slug)
        normalized_business = normalize_text(business)
        if not normalized_business:
            raise InvalidCampaignError("campaign business type is required")

        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO campaigns(slug, business_type, normalized_business_type, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (validated_slug, business, normalized_business, now.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidCampaignError(f"campaign already exists: {validated_slug}") from exc

        return self.get_campaign(validated_slug)

    def get_campaign(self, slug: str) -> CampaignRecord:
        validated_slug = validate_campaign_slug(slug)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM campaigns WHERE slug = ?", (validated_slug,)).fetchone()
        return self._campaign_record_from_row(_require_row(row, f"campaign {validated_slug}"))

    def attach_run(self, slug: str, run_id: str, now: datetime) -> CampaignRecord:
        validated_slug = validate_campaign_slug(slug)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                campaign_row = self._get_campaign_row(connection, validated_slug)
                run = self._run_record_from_row(self._get_run_row(connection, run_id))
                self._ensure_business_type_matches(
                    business_type=run.business_type,
                    normalized_campaign_business_type=str(campaign_row["normalized_business_type"]),
                )

                existing_campaign = connection.execute(
                    "SELECT campaign_slug FROM campaign_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing_campaign is None:
                    connection.execute(
                        """
                        INSERT INTO campaign_runs(campaign_slug, run_id, attached_at)
                        VALUES (?, ?, ?)
                        """,
                        (validated_slug, run_id, now.isoformat()),
                    )
                elif str(existing_campaign["campaign_slug"]) != validated_slug:
                    raise CampaignRunAssignmentError(
                        f"run {run_id} is already attached to {existing_campaign['campaign_slug']}"
                    )

                membership_rows = connection.execute(
                    """
                    SELECT run_businesses.snapshot_json, businesses.canonical_json
                    FROM run_businesses
                    INNER JOIN businesses ON businesses.id = run_businesses.business_id
                    WHERE run_id = ?
                    ORDER BY business_id
                    """,
                    (run_id,),
                ).fetchall()
                for membership_row in membership_rows:
                    snapshot = RunSnapshot.model_validate_json(str(membership_row["snapshot_json"]))
                    self._upsert_campaign_business(
                        connection=connection,
                        campaign_slug=validated_slug,
                        business_id=snapshot.business_id,
                        first_discovered_at=snapshot.first_seen_at,
                        last_discovered_at=snapshot.last_seen_at,
                    )
                    self._seed_cache_from_snapshot(
                        connection=connection,
                        business_id=snapshot.business_id,
                        canonical_json=str(membership_row["canonical_json"]),
                        snapshot=snapshot,
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

        return self._campaign_record_from_row(campaign_row)

    def campaign_for_run(self, run_id: str) -> CampaignRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT campaigns.*
                FROM campaign_runs
                INNER JOIN campaigns ON campaigns.slug = campaign_runs.campaign_slug
                WHERE campaign_runs.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._campaign_record_from_row(row)

    def campaign_status(self, slug: str) -> CampaignStatus:
        campaign = self.get_campaign(slug)
        snapshots = self.campaign_snapshots(campaign.slug)
        with self._connect() as connection:
            run_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM campaign_runs WHERE campaign_slug = ?",
                    (campaign.slug,),
                ).fetchone()[0]
            )
            business_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM campaign_businesses WHERE campaign_slug = ?",
                    (campaign.slug,),
                ).fetchone()[0]
            )

        discovered_in = tuple(sorted({location for snapshot in snapshots for location in snapshot.discovered_in}))
        completed_count = 0
        failed_count = 0
        skipped_count = 0
        pending_count = 0
        for snapshot in snapshots:
            if not _has_http_website(snapshot.website):
                skipped_count += 1
            elif snapshot.enrichment_status is EnrichmentStatus.COMPLETED:
                completed_count += 1
            elif snapshot.enrichment_status is EnrichmentStatus.FAILED:
                failed_count += 1
            else:
                pending_count += 1

        return CampaignStatus(
            campaign=campaign,
            run_count=run_count,
            business_count=business_count,
            discovered_in=discovered_in,
            completed_count=completed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            pending_count=pending_count,
        )

    def campaign_snapshots(self, slug: str) -> tuple[CampaignSnapshot, ...]:
        campaign = self.get_campaign(slug)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    campaign_businesses.business_id,
                    campaign_businesses.first_discovered_at,
                    campaign_businesses.last_discovered_at,
                    businesses.canonical_json,
                    run_businesses.snapshot_json
                FROM campaign_businesses
                INNER JOIN businesses ON businesses.id = campaign_businesses.business_id
                INNER JOIN campaign_runs ON campaign_runs.campaign_slug = campaign_businesses.campaign_slug
                INNER JOIN run_businesses
                    ON run_businesses.run_id = campaign_runs.run_id
                   AND run_businesses.business_id = campaign_businesses.business_id
                WHERE campaign_businesses.campaign_slug = ?
                ORDER BY campaign_businesses.business_id
                """,
                (campaign.slug,),
            ).fetchall()

        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(int(row["business_id"]), []).append(row)

        snapshots: list[CampaignSnapshot] = []
        for business_id, membership_rows in grouped.items():
            canonical = _CanonicalBusiness.model_validate_json(str(membership_rows[0]["canonical_json"]))
            run_snapshots = [
                RunSnapshot.model_validate_json(str(membership_row["snapshot_json"]))
                for membership_row in membership_rows
            ]
            latest_snapshot = max(run_snapshots, key=lambda snapshot: (snapshot.last_seen_at, snapshot.run_id))
            discovered_in = tuple(sorted({snapshot.location_query for snapshot in run_snapshots}))
            cached = self.cached_enrichment(business_id, canonical.website)
            enrichment = cached.result if cached is not None else EnrichmentResult(
                emails=latest_snapshot.emails,
                facebook_url=latest_snapshot.facebook_url,
                instagram_url=latest_snapshot.instagram_url,
                linkedin_url=latest_snapshot.linkedin_url,
                x_url=latest_snapshot.x_url,
                youtube_url=latest_snapshot.youtube_url,
                status=latest_snapshot.enrichment_status,
                error=latest_snapshot.enrichment_error,
            )
            snapshots.append(
                CampaignSnapshot(
                    business_id=business_id,
                    campaign_id=campaign.slug,
                    discovered_in=discovered_in,
                    name=canonical.name or latest_snapshot.name,
                    business_type=campaign.business_type,
                    first_seen_at=_parse_datetime(str(membership_rows[0]["first_discovered_at"])),
                    last_seen_at=_parse_datetime(str(membership_rows[0]["last_discovered_at"])),
                    place_id=canonical.place_id,
                    category=canonical.category,
                    address=canonical.address,
                    phone=canonical.phone,
                    website=canonical.website,
                    rating=canonical.rating,
                    review_count=canonical.review_count,
                    google_maps_url=canonical.google_maps_url,
                    emails=enrichment.emails,
                    facebook_url=enrichment.facebook_url,
                    instagram_url=enrichment.instagram_url,
                    linkedin_url=enrichment.linkedin_url,
                    x_url=enrichment.x_url,
                    youtube_url=enrichment.youtube_url,
                    enrichment_status=enrichment.status,
                    enrichment_error=enrichment.error,
                )
            )

        return tuple(sorted(snapshots, key=_campaign_snapshot_sort_key))

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(f"{_RUN_SELECT} WHERE runs.id = ?", (run_id,)).fetchone()
        return self._run_record_from_row(_require_row(row, f"run {run_id}"))

    def remaining_quota(self, now: datetime) -> int:
        day = self._quota_day(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT accepted_count FROM daily_quota WHERE day = ?",
                (day,),
            ).fetchone()
        accepted_count = 0 if row is None else int(row[0])
        return DAILY_NEW_RECORD_LIMIT - accepted_count

    def new_unique_count_for_run(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT new_unique_count FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return int(_require_row(row, f"run {run_id}")[0])

    def accept_candidate(self, run_id: str, candidate: ProviderCandidate, now: datetime) -> Acceptance:
        identity = build_identity(candidate)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._run_record_from_row(self._get_run_row(connection, run_id))
                keys = (identity.primary_key, *identity.aliases)
                business_id = self._resolve_business_id(connection, keys)
                is_new = business_id is None

                if is_new:
                    self._ensure_quota_available(connection, now)
                    business_id = self._insert_business(connection, candidate, now)
                    first_seen_at = now
                    self._insert_daily_quota_usage(connection, now)
                    connection.execute(
                        "UPDATE runs SET new_unique_count = new_unique_count + 1 WHERE id = ?",
                        (run_id,),
                    )
                else:
                    assert business_id is not None
                    business_row = self._get_business_row(connection, business_id)
                    first_seen_at = _parse_datetime(str(business_row["first_seen_at"]))
                    self._update_business(connection, business_id, candidate, now, business_row)

                assert business_id is not None
                self._store_aliases(connection, business_id, keys)
                snapshot = self._ensure_run_snapshot(
                    connection=connection,
                    run=run,
                    business_id=business_id,
                    candidate=candidate,
                    first_seen_at=first_seen_at,
                    last_seen_at=now,
                )
                if run.campaign_slug is not None:
                    self._upsert_campaign_business(
                        connection=connection,
                        campaign_slug=run.campaign_slug,
                        business_id=business_id,
                        first_discovered_at=now,
                        last_discovered_at=now,
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

        return Acceptance(
            run_id=run_id,
            business_id=business_id,
            is_new=is_new,
            identity=identity,
            snapshot=snapshot,
        )

    def pending_enrichment(self, run_id: str) -> tuple[RunSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_businesses.snapshot_json, businesses.canonical_json
                FROM run_businesses
                INNER JOIN businesses ON businesses.id = run_businesses.business_id
                WHERE run_id = ?
                  AND enrichment_status != ?
                ORDER BY run_businesses.business_id
                """,
                (run_id, EnrichmentStatus.COMPLETED.value),
            ).fetchall()
        snapshots = tuple(self._snapshot_for_pending_enrichment(row) for row in rows)
        return tuple(snapshot for snapshot in snapshots if _has_http_website(snapshot.website))

    def cached_enrichment(
        self,
        business_id: int,
        website: str | None,
    ) -> EnrichmentCacheEntry | None:
        normalized_website = normalize_website_url(website)
        if normalized_website is None:
            return None

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT normalized_website, result_json, completed_at
                FROM business_enrichment_cache
                WHERE business_id = ? AND normalized_website = ?
                """,
                (business_id, normalized_website),
            ).fetchone()
        if row is None:
            return None
        return EnrichmentCacheEntry(
            business_id=business_id,
            normalized_website=str(row["normalized_website"]),
            result=EnrichmentResult.model_validate_json(str(row["result_json"])),
            completed_at=_parse_datetime(str(row["completed_at"])),
        )

    def save_enrichment(
        self,
        run_id: str,
        business_id: int,
        result: EnrichmentResult,
        now: datetime,
    ) -> None:
        del now
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(
                    """
                    SELECT snapshot_json
                    FROM run_businesses
                    WHERE run_id = ? AND business_id = ?
                    """,
                    (run_id, business_id),
                ).fetchone()
                snapshot = RunSnapshot.model_validate_json(
                    str(_require_row(row, f"run business {run_id}/{business_id}")["snapshot_json"])
                )
                updated_snapshot = snapshot.model_copy(
                    update={
                        "emails": result.emails,
                        "facebook_url": result.facebook_url,
                        "instagram_url": result.instagram_url,
                        "linkedin_url": result.linkedin_url,
                        "x_url": result.x_url,
                        "youtube_url": result.youtube_url,
                        "enrichment_status": result.status,
                        "enrichment_error": result.error,
                    }
                )
                connection.execute(
                    """
                    UPDATE run_businesses
                    SET snapshot_json = ?, enrichment_status = ?, enrichment_error = ?
                    WHERE run_id = ? AND business_id = ?
                    """,
                    (
                        updated_snapshot.model_dump_json(),
                        result.status.value,
                        result.error,
                        run_id,
                        business_id,
                    ),
                )

                if result.status is EnrichmentStatus.COMPLETED:
                    business_row = self._get_business_row(connection, business_id)
                    canonical = _CanonicalBusiness.model_validate_json(str(business_row["canonical_json"]))
                    updated_canonical = canonical.model_copy(
                        update={
                            "emails": result.emails,
                            "facebook_url": result.facebook_url,
                            "instagram_url": result.instagram_url,
                            "linkedin_url": result.linkedin_url,
                            "x_url": result.x_url,
                            "youtube_url": result.youtube_url,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE businesses
                        SET canonical_json = ?
                        WHERE id = ?
                        """,
                        (updated_canonical.model_dump_json(), business_id),
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def save_cached_enrichment(
        self,
        business_id: int,
        website: str,
        result: EnrichmentResult,
        now: datetime,
    ) -> None:
        if result.status is not EnrichmentStatus.COMPLETED:
            raise ValueError("cached enrichment requires completed results")
        normalized_website = normalize_website_url(website)
        if normalized_website is None:
            raise ValueError("cached enrichment requires an HTTP(S) website")

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO business_enrichment_cache(
                    business_id,
                    normalized_website,
                    result_json,
                    completed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(business_id) DO UPDATE SET
                    normalized_website = excluded.normalized_website,
                    result_json = excluded.result_json,
                    completed_at = excluded.completed_at
                """,
                (
                    business_id,
                    normalized_website,
                    result.model_dump_json(),
                    now.isoformat(),
                ),
            )

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        finished_at: datetime | None = None,
        error: str | None = None,
    ) -> RunRecord:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, error = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    None if finished_at is None else finished_at.isoformat(),
                    error,
                    run_id,
                ),
            )
            if connection.total_changes == 0:
                raise KeyError(f"run {run_id} not found")
            row = connection.execute(f"{_RUN_SELECT} WHERE runs.id = ?", (run_id,)).fetchone()
        return self._run_record_from_row(_require_row(row, f"run {run_id}"))

    def snapshots_for_run(self, run_id: str) -> tuple[RunSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json
                FROM run_businesses
                WHERE run_id = ?
                ORDER BY business_id
                """,
                (run_id,),
            ).fetchall()
        return tuple(RunSnapshot.model_validate_json(str(row["snapshot_json"])) for row in rows)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection

    def _campaign_record_from_row(self, row: sqlite3.Row) -> CampaignRecord:
        return CampaignRecord(
            slug=str(row["slug"]),
            business_type=str(row["business_type"]),
            created_at=_parse_datetime(str(row["created_at"])),
        )

    def _run_record_from_row(self, row: sqlite3.Row) -> RunRecord:
        campaign_slug = None if row["campaign_slug"] is None else str(row["campaign_slug"])
        refresh_enrichment = bool(int(row["refresh_enrichment"]))
        return RunRecord(
            id=str(row["id"]),
            business_type=str(row["business_type"]),
            location_query=str(row["location_query"]),
            search_query=str(row["search_query"]),
            language=str(row["language"]),
            requested_limit=int(row["requested_limit"]),
            status=RunStatus(str(row["status"])),
            started_at=_parse_datetime(str(row["started_at"])),
            finished_at=None
            if row["finished_at"] is None
            else _parse_datetime(str(row["finished_at"])),
            provider_dir=Path(str(row["provider_dir"])),
            error=None if row["error"] is None else str(row["error"]),
            new_unique_count=int(row["new_unique_count"]),
            campaign_slug=campaign_slug,
            refresh_enrichment=refresh_enrichment,
        )

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE runs ADD COLUMN refresh_enrichment INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            """
            CREATE TABLE campaigns(
                slug TEXT PRIMARY KEY,
                business_type TEXT NOT NULL,
                normalized_business_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE campaign_runs(
                campaign_slug TEXT NOT NULL REFERENCES campaigns(slug),
                run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                attached_at TEXT NOT NULL,
                PRIMARY KEY(campaign_slug, run_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE campaign_businesses(
                campaign_slug TEXT NOT NULL REFERENCES campaigns(slug),
                business_id INTEGER NOT NULL REFERENCES businesses(id),
                first_discovered_at TEXT NOT NULL,
                last_discovered_at TEXT NOT NULL,
                PRIMARY KEY(campaign_slug, business_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE business_enrichment_cache(
                business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
                normalized_website TEXT NOT NULL,
                result_json TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE runs ADD COLUMN search_query TEXT NOT NULL DEFAULT ''")
        connection.execute("ALTER TABLE runs ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")
        connection.execute(
            """
            UPDATE runs
            SET search_query = business_type
            WHERE search_query = ''
            """
        )
        connection.execute(
            """
            UPDATE runs
            SET language = 'en'
            WHERE language = ''
            """
        )

    def _quota_day(self, now: datetime) -> str:
        return now.astimezone(self._settings.timezone).date().isoformat()

    def _get_campaign_row(self, connection: sqlite3.Connection, slug: str) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM campaigns WHERE slug = ?",
            (slug,),
        ).fetchone()
        if row is None:
            raise CampaignNotFoundError(f"campaign {slug} not found")
        return row

    def _get_run_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            f"{_RUN_SELECT} WHERE runs.id = ?",
            (run_id,),
        ).fetchone()
        return _require_row(row, f"run {run_id}")

    def _resolve_business_id(
        self,
        connection: sqlite3.Connection,
        keys: tuple[str, ...],
    ) -> int | None:
        placeholders = ", ".join("?" for _ in keys)
        rows = connection.execute(
            f"SELECT alias, business_id FROM identity_aliases WHERE alias IN ({placeholders})",
            keys,
        ).fetchall()
        by_alias = {str(row["alias"]): int(row["business_id"]) for row in rows}
        for key in keys:
            if key in by_alias:
                return by_alias[key]
        return None

    def _ensure_business_type_matches(
        self,
        *,
        business_type: str,
        normalized_campaign_business_type: str,
    ) -> None:
        if normalize_text(business_type) != normalized_campaign_business_type:
            raise CampaignBusinessTypeError("run business type does not match campaign")

    def _ensure_quota_available(self, connection: sqlite3.Connection, now: datetime) -> None:
        day = self._quota_day(now)
        row = connection.execute(
            "SELECT accepted_count FROM daily_quota WHERE day = ?",
            (day,),
        ).fetchone()
        accepted_count = 0 if row is None else int(row[0])
        if accepted_count >= DAILY_NEW_RECORD_LIMIT:
            raise QuotaExceededError(f"daily quota exhausted for {day}")

    def _insert_business(
        self,
        connection: sqlite3.Connection,
        candidate: ProviderCandidate,
        now: datetime,
    ) -> int:
        canonical = _CanonicalBusiness(
            place_id=candidate.place_id,
            name=candidate.name,
            category=candidate.category,
            address=candidate.address,
            phone=candidate.phone,
            website=candidate.website,
            rating=candidate.rating,
            review_count=candidate.review_count,
            google_maps_url=candidate.google_maps_url,
        )
        cursor = connection.execute(
            """
            INSERT INTO businesses(place_id, canonical_json, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                canonical.place_id,
                canonical.model_dump_json(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        business_id = cursor.lastrowid
        if business_id is None:
            raise RuntimeError("failed to insert business")
        return int(business_id)

    def _get_business_row(self, connection: sqlite3.Connection, business_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
        return _require_row(row, f"business {business_id}")

    def _update_business(
        self,
        connection: sqlite3.Connection,
        business_id: int,
        candidate: ProviderCandidate,
        now: datetime,
        row: sqlite3.Row,
    ) -> None:
        canonical = _CanonicalBusiness.model_validate_json(str(row["canonical_json"]))
        merged = canonical.model_copy(
            update={
                "place_id": canonical.place_id or candidate.place_id,
                "name": _prefer_candidate_value(candidate.name, canonical.name),
                "category": _prefer_candidate_value(candidate.category, canonical.category),
                "address": _prefer_candidate_value(candidate.address, canonical.address),
                "phone": _prefer_candidate_value(candidate.phone, canonical.phone),
                "website": _prefer_candidate_value(candidate.website, canonical.website),
                "rating": candidate.rating if candidate.rating is not None else canonical.rating,
                "review_count": (
                    candidate.review_count
                    if candidate.review_count is not None
                    else canonical.review_count
                ),
                "google_maps_url": _prefer_candidate_value(
                    candidate.google_maps_url,
                    canonical.google_maps_url,
                ),
            }
        )
        connection.execute(
            """
            UPDATE businesses
            SET place_id = ?, canonical_json = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                merged.place_id,
                merged.model_dump_json(),
                now.isoformat(),
                business_id,
            ),
        )

    def _insert_daily_quota_usage(self, connection: sqlite3.Connection, now: datetime) -> None:
        day = self._quota_day(now)
        connection.execute(
            """
            INSERT INTO daily_quota(day, accepted_count)
            VALUES (?, 1)
            ON CONFLICT(day) DO UPDATE SET accepted_count = accepted_count + 1
            """,
            (day,),
        )

    def _store_aliases(
        self,
        connection: sqlite3.Connection,
        business_id: int,
        keys: tuple[str, ...],
    ) -> None:
        for key in keys:
            row = connection.execute(
                "SELECT business_id FROM identity_aliases WHERE alias = ?",
                (key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO identity_aliases(alias, business_id) VALUES (?, ?)",
                    (key, business_id),
                )
                continue

            existing_business_id = int(row[0])
            if existing_business_id != business_id:
                continue

    def _ensure_run_snapshot(
        self,
        *,
        connection: sqlite3.Connection,
        run: RunRecord,
        business_id: int,
        candidate: ProviderCandidate,
        first_seen_at: datetime,
        last_seen_at: datetime,
    ) -> RunSnapshot:
        row = connection.execute(
            """
            SELECT snapshot_json
            FROM run_businesses
            WHERE run_id = ? AND business_id = ?
            """,
            (run.id, business_id),
        ).fetchone()
        if row is not None:
            existing = RunSnapshot.model_validate_json(str(row["snapshot_json"]))
            updated = existing.model_copy(
                update={
                    "name": _prefer_candidate_value(candidate.name, existing.name),
                    "category": _prefer_candidate_value(candidate.category, existing.category),
                    "address": _prefer_candidate_value(candidate.address, existing.address),
                    "phone": _prefer_candidate_value(candidate.phone, existing.phone),
                    "website": _prefer_candidate_value(candidate.website, existing.website),
                    "rating": candidate.rating if candidate.rating is not None else existing.rating,
                    "review_count": (
                        candidate.review_count
                        if candidate.review_count is not None
                        else existing.review_count
                    ),
                    "google_maps_url": _prefer_candidate_value(
                        candidate.google_maps_url,
                        existing.google_maps_url,
                    ),
                    "last_seen_at": last_seen_at,
                }
            )
            if _normalized_website_changed(existing.website, updated.website):
                updated = updated.model_copy(
                    update={
                        "emails": (),
                        "facebook_url": None,
                        "instagram_url": None,
                        "linkedin_url": None,
                        "x_url": None,
                        "youtube_url": None,
                        "enrichment_status": EnrichmentStatus.PENDING,
                        "enrichment_error": None,
                    }
                )
            connection.execute(
                """
                UPDATE run_businesses
                SET snapshot_json = ?, enrichment_status = ?, enrichment_error = ?
                WHERE run_id = ? AND business_id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.enrichment_status.value,
                    updated.enrichment_error,
                    run.id,
                    business_id,
                ),
            )
            return updated

        snapshot = RunSnapshot(
            business_id=business_id,
            run_id=run.id,
            name=candidate.name or "",
            business_type=run.business_type,
            location_query=run.location_query,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            place_id=candidate.place_id,
            category=candidate.category,
            address=candidate.address,
            phone=candidate.phone,
            website=candidate.website,
            rating=candidate.rating,
            review_count=candidate.review_count,
            google_maps_url=candidate.google_maps_url,
            enrichment_status=EnrichmentStatus.PENDING,
            enrichment_error=None,
        )
        connection.execute(
            """
            INSERT INTO run_businesses(
                run_id,
                business_id,
                snapshot_json,
                enrichment_status,
                enrichment_error
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run.id,
                business_id,
                snapshot.model_dump_json(),
                snapshot.enrichment_status.value,
                snapshot.enrichment_error,
            ),
        )
        return snapshot

    def _upsert_campaign_business(
        self,
        *,
        connection: sqlite3.Connection,
        campaign_slug: str,
        business_id: int,
        first_discovered_at: datetime,
        last_discovered_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO campaign_businesses(
                campaign_slug,
                business_id,
                first_discovered_at,
                last_discovered_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(campaign_slug, business_id) DO UPDATE SET
                first_discovered_at = MIN(first_discovered_at, excluded.first_discovered_at),
                last_discovered_at = MAX(last_discovered_at, excluded.last_discovered_at)
            """,
            (
                campaign_slug,
                business_id,
                first_discovered_at.isoformat(),
                last_discovered_at.isoformat(),
            ),
        )

    def _snapshot_for_pending_enrichment(self, row: sqlite3.Row) -> RunSnapshot:
        snapshot = RunSnapshot.model_validate_json(str(row["snapshot_json"]))
        if _has_http_website(snapshot.website):
            return snapshot

        canonical = _CanonicalBusiness.model_validate_json(str(row["canonical_json"]))
        if not _has_http_website(canonical.website):
            return snapshot

        return snapshot.model_copy(update={"website": canonical.website})

    def _seed_cache_from_snapshot(
        self,
        *,
        connection: sqlite3.Connection,
        business_id: int,
        canonical_json: str,
        snapshot: RunSnapshot,
    ) -> None:
        if snapshot.enrichment_status is not EnrichmentStatus.COMPLETED:
            return
        canonical = _CanonicalBusiness.model_validate_json(canonical_json)
        normalized_canonical_website = normalize_website_url(canonical.website)
        normalized_snapshot_website = normalize_website_url(snapshot.website)
        if normalized_canonical_website is None or normalized_snapshot_website != normalized_canonical_website:
            return

        result = EnrichmentResult(
            status=snapshot.enrichment_status,
            emails=snapshot.emails,
            facebook_url=snapshot.facebook_url,
            instagram_url=snapshot.instagram_url,
            linkedin_url=snapshot.linkedin_url,
            x_url=snapshot.x_url,
            youtube_url=snapshot.youtube_url,
            error=snapshot.enrichment_error,
        )
        connection.execute(
            """
            INSERT INTO business_enrichment_cache(
                business_id,
                normalized_website,
                result_json,
                completed_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(business_id) DO UPDATE SET
                normalized_website = excluded.normalized_website,
                result_json = excluded.result_json,
                completed_at = excluded.completed_at
            """,
            (
                business_id,
                normalized_canonical_website,
                result.model_dump_json(),
                snapshot.last_seen_at.isoformat(),
            ),
        )


def _campaign_snapshot_sort_key(snapshot: CampaignSnapshot) -> tuple[str, str, int]:
    return (
        normalize_text(snapshot.name),
        normalize_text(snapshot.address),
        snapshot.business_id,
    )


def _has_http_website(value: str | None) -> bool:
    return normalize_website_url(value) is not None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _prefer_candidate_value(candidate_value: str | None, current_value: str | None) -> str | None:
    return candidate_value if candidate_value is not None else current_value


def _normalized_website_changed(previous: str | None, current: str | None) -> bool:
    return normalize_website_url(previous) != normalize_website_url(current)


def _require_row(row: sqlite3.Row | None, label: str) -> sqlite3.Row:
    if row is None:
        raise KeyError(f"{label} not found")
    return row
