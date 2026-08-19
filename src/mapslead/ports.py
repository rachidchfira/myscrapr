from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from mapslead.models import (
    Acceptance,
    CampaignRecord,
    CampaignSnapshot,
    CampaignStatus,
    EnrichmentCacheEntry,
    EnrichmentResult,
    ExportPaths,
    ProgressEvent,
    ProviderCandidate,
    ProviderRequest,
    ProviderResult,
    RunRecord,
    RunSnapshot,
    RunStatus,
)


class CandidateSink(Protocol):
    def __call__(self, candidate: ProviderCandidate) -> None: ...


class MapsProvider(Protocol):
    def replay(self, request: ProviderRequest, sink: CandidateSink) -> ProviderResult: ...

    def acquire(self, request: ProviderRequest, sink: CandidateSink) -> ProviderResult: ...


class FetchedPageLike(Protocol):
    final_url: str
    html: str


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPageLike: ...


class WebsiteEnricher(Protocol):
    def enrich(self, website: str) -> EnrichmentResult: ...


class ExporterPort(Protocol):
    def export_run(self, run_id: str) -> ExportPaths: ...


class CampaignExporterPort(Protocol):
    def export_campaign(self, slug: str) -> ExportPaths: ...


class ProgressSink(Protocol):
    def __call__(self, event: ProgressEvent) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class RepositoryPort(Protocol):
    def initialize(self) -> None: ...

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
    ) -> RunRecord: ...

    def create_campaign(self, slug: str, business: str, now: datetime) -> CampaignRecord: ...

    def get_campaign(self, slug: str) -> CampaignRecord: ...

    def attach_run(self, slug: str, run_id: str, now: datetime) -> CampaignRecord: ...

    def campaign_for_run(self, run_id: str) -> CampaignRecord | None: ...

    def campaign_status(self, slug: str) -> CampaignStatus: ...

    def campaign_snapshots(self, slug: str) -> Sequence[CampaignSnapshot]: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def remaining_quota(self, now: datetime) -> int: ...

    def new_unique_count_for_run(self, run_id: str) -> int: ...

    def accept_candidate(self, run_id: str, candidate: ProviderCandidate, now: datetime) -> Acceptance: ...

    def pending_enrichment(self, run_id: str) -> Sequence[RunSnapshot]: ...

    def cached_enrichment(
        self,
        business_id: int,
        website: str | None,
    ) -> EnrichmentCacheEntry | None: ...

    def save_enrichment(
        self,
        run_id: str,
        business_id: int,
        result: EnrichmentResult,
        now: datetime,
    ) -> None: ...

    def save_cached_enrichment(
        self,
        business_id: int,
        website: str,
        result: EnrichmentResult,
        now: datetime,
    ) -> None: ...

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        finished_at: datetime | None = None,
        error: str | None = None,
    ) -> RunRecord: ...

    def snapshots_for_run(self, run_id: str) -> Sequence[RunSnapshot]: ...
