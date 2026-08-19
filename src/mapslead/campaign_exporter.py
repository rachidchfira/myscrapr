from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

from mapslead.config import Settings
from mapslead.errors import ExportError
from mapslead.exporter import (
    _cleanup_path,
    _replace_export_pair,
    _replace_target,
    _write_atomic_temp,
)
from mapslead.models import CampaignSnapshot, ExportPaths
from mapslead.normalize import normalize_text, validate_campaign_slug
from mapslead.ports import CampaignExporterPort, RepositoryPort

CAMPAIGN_CSV_FIELDS = (
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
    "first_seen_at",
    "last_seen_at",
    "enrichment_status",
    "enrichment_error",
    "campaign_id",
    "discovered_in",
)


@dataclass(frozen=True, slots=True)
class _PreparedCampaignRow:
    business_id: int
    place_id: str | None
    name: str
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
    business_type: str
    first_seen_at: str
    last_seen_at: str
    enrichment_status: str
    enrichment_error: str | None
    campaign_id: str
    discovered_in: tuple[str, ...]


class CampaignExporter(CampaignExporterPort):
    def __init__(self, repository: RepositoryPort, settings: Settings) -> None:
        self._repository = repository
        self._export_dir = settings.export_dir

    def export_campaign(self, slug: str) -> ExportPaths:
        campaign_dir = Path(os.fspath(_validated_campaign_dir(self._export_dir, slug)))
        snapshots = tuple(
            sorted(
                self._repository.campaign_snapshots(slug),
                key=lambda snapshot: (
                    normalize_text(snapshot.name),
                    normalize_text(snapshot.address),
                    snapshot.business_id,
                ),
            )
        )
        rows = tuple(_prepare_row(snapshot) for snapshot in snapshots)

        campaign_dir.mkdir(parents=True, exist_ok=True)

        csv_path = campaign_dir / "results.csv"
        json_path = campaign_dir / "results.json"
        csv_temp_path = campaign_dir / "results.csv.tmp"
        json_temp_path = campaign_dir / "results.json.tmp"

        try:
            _write_atomic_temp(csv_temp_path, _build_csv_document(rows))
            _write_atomic_temp(json_temp_path, _build_json_document(rows))
            _replace_export_pair(
                (
                    _replace_target(csv_temp_path, csv_path),
                    _replace_target(json_temp_path, json_path),
                )
            )
        except Exception as error:
            _cleanup_path(csv_temp_path)
            _cleanup_path(json_temp_path)
            raise ExportError(f"failed to export campaign {slug}") from error

        return ExportPaths(csv_path=csv_path, json_path=json_path)


def _prepare_row(snapshot: CampaignSnapshot) -> _PreparedCampaignRow:
    return _PreparedCampaignRow(
        business_id=snapshot.business_id,
        place_id=snapshot.place_id,
        name=snapshot.name,
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
        business_type=snapshot.business_type,
        first_seen_at=snapshot.first_seen_at.isoformat(),
        last_seen_at=snapshot.last_seen_at.isoformat(),
        enrichment_status=snapshot.enrichment_status.value,
        enrichment_error=snapshot.enrichment_error,
        campaign_id=snapshot.campaign_id,
        discovered_in=tuple(sorted(snapshot.discovered_in)),
    )


def _build_csv_document(rows: tuple[_PreparedCampaignRow, ...]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CAMPAIGN_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_row(row))
    return buffer.getvalue()


def _csv_row(row: _PreparedCampaignRow) -> dict[str, str | int | float | None]:
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
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "enrichment_status": row.enrichment_status,
        "enrichment_error": row.enrichment_error,
        "campaign_id": row.campaign_id,
        "discovered_in": ";".join(row.discovered_in),
    }


def _build_json_document(rows: tuple[_PreparedCampaignRow, ...]) -> str:
    return json.dumps(
        [_json_row(row) for row in rows],
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _json_row(row: _PreparedCampaignRow) -> dict[str, str | int | float | list[str] | None]:
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
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "enrichment_status": row.enrichment_status,
        "enrichment_error": row.enrichment_error,
        "campaign_id": row.campaign_id,
        "discovered_in": list(row.discovered_in),
    }


def _validated_campaign_dir(export_root: Path, slug: str) -> Path:
    validated_slug = validate_campaign_slug(slug)
    resolved_export_root = export_root.resolve(strict=False)
    campaigns_root_path = export_root / "campaigns"
    campaigns_root = campaigns_root_path.resolve(strict=False)
    if campaigns_root.parent != resolved_export_root:
        raise ExportError(f"unsafe campaign slug: {slug!r}")
    campaign_dir = (export_root / "campaigns" / validated_slug).resolve(strict=False)
    if campaign_dir.parent != campaigns_root:
        raise ExportError(f"unsafe campaign slug: {slug!r}")
    return campaign_dir
