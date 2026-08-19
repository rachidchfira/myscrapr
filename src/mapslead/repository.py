from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from mapslead.config import DAILY_NEW_RECORD_LIMIT, Settings
from mapslead.errors import QuotaExceededError
from mapslead.models import (
    Acceptance,
    EnrichmentResult,
    EnrichmentStatus,
    ProviderCandidate,
    RunRecord,
    RunSnapshot,
    RunStatus,
)
from mapslead.normalize import build_identity

_SCHEMA_VERSION = 1
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
            version = _require_row(row, "schema_version")[0]
            if int(version) != _SCHEMA_VERSION:
                raise RuntimeError(f"unsupported schema version: {version}")

    def create_run(
        self,
        business: str,
        location: str,
        requested_limit: int,
        now: datetime,
    ) -> RunRecord:
        run_id = uuid4().hex
        provider_dir = self._settings.data_dir / "runs" / run_id / "provider"
        provider_dir.mkdir(parents=True, exist_ok=True)

        with self._connect() as connection:
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
                    run_id,
                    business,
                    location,
                    requested_limit,
                    RunStatus.RUNNING.value,
                    now.isoformat(),
                    None,
                    str(provider_dir),
                    None,
                    0,
                ),
            )

        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
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
                SELECT snapshot_json
                FROM run_businesses
                WHERE run_id = ?
                  AND enrichment_status != ?
                ORDER BY business_id
                """,
                (run_id, EnrichmentStatus.COMPLETED.value),
            ).fetchall()
        snapshots = tuple(RunSnapshot.model_validate_json(str(row["snapshot_json"])) for row in rows)
        return tuple(snapshot for snapshot in snapshots if _has_http_website(snapshot.website))

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
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
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

    def _run_record_from_row(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=str(row["id"]),
            business_type=str(row["business_type"]),
            location_query=str(row["location_query"]),
            requested_limit=int(row["requested_limit"]),
            status=RunStatus(str(row["status"])),
            started_at=_parse_datetime(str(row["started_at"])),
            finished_at=None
            if row["finished_at"] is None
            else _parse_datetime(str(row["finished_at"])),
            provider_dir=Path(str(row["provider_dir"])),
            error=None if row["error"] is None else str(row["error"]),
            new_unique_count=int(row["new_unique_count"]),
        )

    def _quota_day(self, now: datetime) -> str:
        return now.astimezone(self._settings.timezone).date().isoformat()

    def _get_run_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
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
            return RunSnapshot.model_validate_json(str(row["snapshot_json"]))

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


def _has_http_website(value: str | None) -> bool:
    if value is None:
        return False
    lowered = value.casefold()
    return lowered.startswith(("http://", "https://"))


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _prefer_candidate_value(candidate_value: str | None, current_value: str | None) -> str | None:
    return candidate_value if candidate_value is not None else current_value


def _require_row(row: sqlite3.Row | None, label: str) -> sqlite3.Row:
    if row is None:
        raise KeyError(f"{label} not found")
    return row
