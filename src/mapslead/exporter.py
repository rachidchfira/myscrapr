from __future__ import annotations

import csv
import io
import json
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from mapslead.config import Settings
from mapslead.errors import ExportError
from mapslead.models import ExportPaths, RunSnapshot
from mapslead.normalize import normalize_text
from mapslead.ports import RepositoryPort

_CSV_FIELDNAMES = (
    "place_id",
    "name",
    "category",
    "address",
    "phone",
    "website",
    "rating",
    "review_count",
    "google_maps_url",
    "emails",
    "facebook_url",
    "instagram_url",
    "linkedin_url",
    "x_url",
    "youtube_url",
    "business_type",
    "location_query",
    "first_seen_at",
    "last_seen_at",
    "enrichment_status",
    "enrichment_error",
    "run_id",
)


@dataclass(frozen=True, slots=True)
class _PreparedRow:
    business_id: int
    run_id: str
    name: str
    business_type: str
    location_query: str
    first_seen_at: str
    last_seen_at: str
    place_id: str | None
    category: str | None
    address: str | None
    phone: str | None
    website: str | None
    rating: float | None
    review_count: int | None
    google_maps_url: str | None
    emails: tuple[str, ...]
    facebook_url: str | None
    instagram_url: str | None
    linkedin_url: str | None
    x_url: str | None
    youtube_url: str | None
    enrichment_status: str
    enrichment_error: str | None


@dataclass(frozen=True, slots=True)
class _ReplaceTarget:
    temp_path: Path
    destination_path: Path
    backup_path: Path
    destination_existed: bool


class Exporter:
    def __init__(self, repository: RepositoryPort, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def export_run(self, run_id: str) -> ExportPaths:
        run_dir = _validated_run_dir(self._settings.export_dir, run_id)
        snapshots = tuple(
            sorted(
                self._repository.snapshots_for_run(run_id),
                key=lambda snapshot: (
                    normalize_text(snapshot.name),
                    normalize_text(snapshot.address),
                    snapshot.business_id,
                ),
            )
        )
        rows = tuple(_prepare_row(snapshot) for snapshot in snapshots)

        run_dir.mkdir(parents=True, exist_ok=True)

        csv_path = run_dir / "results.csv"
        json_path = run_dir / "results.json"
        csv_temp_path = run_dir / "results.csv.tmp"
        json_temp_path = run_dir / "results.json.tmp"

        try:
            csv_document = _build_csv_document(rows)
            _write_atomic_temp(csv_temp_path, csv_document)

            json_document = _build_json_document(rows)
            _write_atomic_temp(json_temp_path, json_document)

            _replace_export_pair(
                (
                    _replace_target(csv_temp_path, csv_path),
                    _replace_target(json_temp_path, json_path),
                )
            )
        except Exception as error:
            _cleanup_path(csv_temp_path)
            _cleanup_path(json_temp_path)
            raise ExportError(f"failed to export run {run_id}") from error

        return ExportPaths(csv_path=csv_path, json_path=json_path)


def _prepare_row(snapshot: RunSnapshot) -> _PreparedRow:
    return _PreparedRow(
        business_id=snapshot.business_id,
        run_id=snapshot.run_id,
        name=snapshot.name,
        business_type=snapshot.business_type,
        location_query=snapshot.location_query,
        first_seen_at=snapshot.first_seen_at.isoformat(),
        last_seen_at=snapshot.last_seen_at.isoformat(),
        place_id=snapshot.place_id,
        category=snapshot.category,
        address=snapshot.address,
        phone=snapshot.phone,
        website=snapshot.website,
        rating=snapshot.rating,
        review_count=snapshot.review_count,
        google_maps_url=snapshot.google_maps_url,
        emails=tuple(sorted(snapshot.emails)),
        facebook_url=snapshot.facebook_url,
        instagram_url=snapshot.instagram_url,
        linkedin_url=snapshot.linkedin_url,
        x_url=snapshot.x_url,
        youtube_url=snapshot.youtube_url,
        enrichment_status=snapshot.enrichment_status.value,
        enrichment_error=snapshot.enrichment_error,
    )


def _build_csv_document(rows: tuple[_PreparedRow, ...]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_row(row))
    return buffer.getvalue()


def _csv_row(row: _PreparedRow) -> dict[str, str | int | float | None]:
    return {
        "place_id": row.place_id,
        "name": row.name,
        "category": row.category,
        "address": row.address,
        "phone": row.phone,
        "website": row.website,
        "rating": row.rating,
        "review_count": row.review_count,
        "google_maps_url": row.google_maps_url,
        "emails": ";".join(row.emails),
        "facebook_url": row.facebook_url,
        "instagram_url": row.instagram_url,
        "linkedin_url": row.linkedin_url,
        "x_url": row.x_url,
        "youtube_url": row.youtube_url,
        "business_type": row.business_type,
        "location_query": row.location_query,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "enrichment_status": row.enrichment_status,
        "enrichment_error": row.enrichment_error,
        "run_id": row.run_id,
    }


def _build_json_document(rows: tuple[_PreparedRow, ...]) -> str:
    return json.dumps(
        [_json_row(row) for row in rows],
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _json_row(row: _PreparedRow) -> dict[str, str | int | float | list[str] | None]:
    return {
        "place_id": row.place_id,
        "name": row.name,
        "category": row.category,
        "address": row.address,
        "phone": row.phone,
        "website": row.website,
        "rating": row.rating,
        "review_count": row.review_count,
        "google_maps_url": row.google_maps_url,
        "emails": list(row.emails),
        "facebook_url": row.facebook_url,
        "instagram_url": row.instagram_url,
        "linkedin_url": row.linkedin_url,
        "x_url": row.x_url,
        "youtube_url": row.youtube_url,
        "business_type": row.business_type,
        "location_query": row.location_query,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "enrichment_status": row.enrichment_status,
        "enrichment_error": row.enrichment_error,
        "run_id": row.run_id,
    }


def _write_atomic_temp(path: Path, document: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_target(temp_path: Path, destination_path: Path) -> _ReplaceTarget:
    suffix = uuid.uuid4().hex
    return _ReplaceTarget(
        temp_path=temp_path,
        destination_path=destination_path,
        backup_path=destination_path.with_name(f"{destination_path.name}.{suffix}.bak"),
        destination_existed=destination_path.exists(),
    )


def _replace_export_pair(targets: tuple[_ReplaceTarget, _ReplaceTarget]) -> None:
    prepared: list[_ReplaceTarget] = []
    replaced: list[_ReplaceTarget] = []
    directory = targets[0].destination_path.parent
    try:
        for target in targets:
            if target.destination_existed:
                os.replace(target.destination_path, target.backup_path)
            prepared.append(target)

        for target in targets:
            os.replace(target.temp_path, target.destination_path)
            replaced.append(target)

        _fsync_directory(directory)
    except Exception:
        for target in reversed(replaced):
            if target.destination_existed:
                with suppress(FileNotFoundError):
                    os.replace(target.backup_path, target.destination_path)
            else:
                _cleanup_path(target.destination_path)

        for target in reversed(prepared):
            if target in replaced:
                continue
            if target.destination_existed:
                with suppress(FileNotFoundError):
                    os.replace(target.backup_path, target.destination_path)

        _fsync_directory(directory)
        raise
    finally:
        for target in targets:
            _cleanup_path(target.temp_path)
            _cleanup_path(target.backup_path)


def _cleanup_path(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return

    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_run_dir(export_root: Path, run_id: str) -> Path:
    if not run_id or run_id in {".", ".."}:
        raise ExportError(f"unsafe run_id: {run_id!r}")

    run_path = Path(run_id)
    if run_path.is_absolute() or "/" in run_id or "\\" in run_id or len(run_path.parts) != 1:
        raise ExportError(f"unsafe run_id: {run_id!r}")

    resolved_export_root = export_root.resolve(strict=False)
    resolved_run_dir = (resolved_export_root / run_id).resolve(strict=False)
    if resolved_run_dir.parent != resolved_export_root:
        raise ExportError(f"unsafe run_id: {run_id!r}")

    return resolved_run_dir
