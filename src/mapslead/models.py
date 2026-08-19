from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class EnrichmentStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderCandidate(FrozenModel):
    place_id: str | None = None
    name: str | None = None
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    review_count: int | None = None
    google_maps_url: str | None = None


class Identity(FrozenModel):
    primary_key: str
    aliases: tuple[str, ...] = ()


class ProviderRequest(FrozenModel):
    business: str
    location: str
    provider_dir: Path
    max_new_records: int = Field(ge=1)


class ProviderResult(FrozenModel):
    status: Literal["completed", "partial", "blocked", "failed"]
    candidate_count: int
    rejected_row_count: int
    diagnostics_tail: str
    interrupted: bool = False


class EnrichmentResult(FrozenModel):
    status: EnrichmentStatus = EnrichmentStatus.PENDING
    emails: tuple[str, ...] = ()
    facebook_url: str | None = None
    instagram_url: str | None = None
    linkedin_url: str | None = None
    x_url: str | None = None
    youtube_url: str | None = None
    error: str | None = None


class CampaignRecord(FrozenModel):
    slug: str
    business_type: str
    created_at: datetime


class EnrichmentCacheEntry(FrozenModel):
    business_id: int
    normalized_website: str
    result: EnrichmentResult
    completed_at: datetime


class RunSnapshot(FrozenModel):
    business_id: int
    run_id: str
    name: str
    business_type: str
    location_query: str
    first_seen_at: datetime
    last_seen_at: datetime
    place_id: str | None = None
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
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    enrichment_error: str | None = None


class CampaignSnapshot(FrozenModel):
    business_id: int
    campaign_id: str
    discovered_in: tuple[str, ...]
    name: str
    business_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    place_id: str | None = None
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
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    enrichment_error: str | None = None


class Acceptance(FrozenModel):
    run_id: str
    business_id: int
    is_new: bool
    identity: Identity
    snapshot: RunSnapshot | None = None


class CampaignStatus(FrozenModel):
    campaign: CampaignRecord
    run_count: int = Field(ge=0)
    business_count: int = Field(ge=0)
    discovered_in: tuple[str, ...] = ()
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)


class RunRecord(FrozenModel):
    id: str
    business_type: str
    location_query: str
    requested_limit: int = Field(ge=1)
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    provider_dir: Path
    error: str | None = None
    new_unique_count: int = Field(default=0, ge=0)
    campaign_slug: str | None = None
    refresh_enrichment: bool = False


class ExportPaths(FrozenModel):
    csv_path: Path
    json_path: Path


class ProgressEvent(FrozenModel):
    kind: str
    message: str
    run_id: str | None = None
    candidate_count: int | None = None
    new_unique_count: int | None = None
    completed_count: int | None = None
    total_count: int | None = None
    export_paths: ExportPaths | None = None
