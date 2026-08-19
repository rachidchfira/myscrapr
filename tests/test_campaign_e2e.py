from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mapslead.campaign_exporter import CampaignExporter
from mapslead.config import DAILY_NEW_RECORD_LIMIT, Settings
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
from mapslead.service import MapsLeadService


@dataclass(frozen=True, slots=True)
class ProviderScript:
    candidates: tuple[ProviderCandidate, ...]
    result: ProviderResult


@dataclass(slots=True)
class FakeProvider:
    acquire_scripts: list[ProviderScript] = field(default_factory=list)
    acquire_requests: list[ProviderRequest] = field(default_factory=list)

    def acquire(self, request: ProviderRequest, sink: Any) -> ProviderResult:
        self.acquire_requests.append(request)
        script = self.acquire_scripts.pop(0)
        for candidate in script.candidates:
            sink(candidate)
        return script.result

    def replay(self, request: ProviderRequest, sink: Any) -> ProviderResult:
        del request, sink
        return ProviderResult(
            status="completed",
            candidate_count=0,
            rejected_row_count=0,
            diagnostics_tail="",
        )


@dataclass(slots=True)
class FakeEnricher:
    responses: dict[str, EnrichmentResult] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def enrich(self, website: str) -> EnrichmentResult:
        self.calls.append(website)
        return self.responses[website]


def _progress_sink(events: list[ProgressEvent]) -> Any:
    def sink(event: ProgressEvent) -> None:
        events.append(event)

    return sink


def test_offline_national_campaign_flow_reuses_cache_and_exports_deduplicated_rows(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")
    repository = SQLiteRepository(settings)
    repository.initialize()
    exporter = Exporter(repository, settings)
    campaign_exporter = CampaignExporter(repository, settings)
    now = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)

    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)

    legacy_candidate = ProviderCandidate(
        place_id="place-1",
        name="Example Dental",
        category="Dentist",
        address="1 Main St",
        phone="+84 28 123 456",
        website="https://example.com",
        rating=4.8,
        review_count=19,
        google_maps_url="https://maps.google.com/?cid=place-1",
    )
    legacy_run = repository.create_run("dentists", "Ho Chi Minh City", 5, now)
    legacy_acceptance = repository.accept_candidate(legacy_run.id, legacy_candidate, now)
    repository.save_enrichment(
        legacy_run.id,
        legacy_acceptance.business_id,
        EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=("hello@example.com",),
        ),
        now,
    )
    repository.set_run_status(legacy_run.id, RunStatus.COMPLETED, finished_at=now)

    repository.attach_run(campaign.slug, legacy_run.id, now)
    cached = repository.cached_enrichment(legacy_acceptance.business_id, legacy_candidate.website)
    assert cached is not None
    assert cached.result.emails == ("hello@example.com",)

    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(
                    legacy_candidate,
                    ProviderCandidate(
                        place_id="place-2",
                        name="Bright Dental",
                        category="Dentist",
                        address="2 Main St",
                        phone="+84 28 654 321",
                        website="https://bright.example.com",
                        rating=4.6,
                        review_count=11,
                        google_maps_url="https://maps.google.com/?cid=place-2",
                    ),
                ),
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
            "https://bright.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("contact@bright.example.com",),
            )
        }
    )
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

    assert outcome.run.status is RunStatus.COMPLETED
    assert repository.remaining_quota(now) == DAILY_NEW_RECORD_LIMIT - 2
    assert enricher.calls == ["https://bright.example.com"]
    assert [event.kind for event in progress_events] == [
        "acquisition",
        "acquisition",
        "enrichment_reused",
        "enrichment",
        "export",
    ]

    export_paths = campaign_exporter.export_campaign(campaign.slug)
    payload = json.loads(export_paths.json_path.read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert payload[0]["name"] == "Bright Dental"
    assert payload[1]["name"] == "Example Dental"
    assert payload[1]["discovered_in"] == ["Hanoi", "Ho Chi Minh City"]
    assert payload[1]["emails"] == ["hello@example.com"]

    first_csv = export_paths.csv_path.read_bytes()
    first_json = export_paths.json_path.read_bytes()
    second_export_paths = campaign_exporter.export_campaign(campaign.slug)
    assert second_export_paths.csv_path.read_bytes() == first_csv
    assert second_export_paths.json_path.read_bytes() == first_json

    restaurants = repository.create_campaign("vietnam-restaurants", "restaurants", now)
    restaurant_run = repository.create_run("restaurants", "Da Nang", 5, now, campaign_slug=restaurants.slug)
    repository.accept_candidate(
        restaurant_run.id,
        ProviderCandidate(
            place_id="place-3",
            name="Rice House",
            category="Restaurant",
            address="3 Main St",
            phone="+84 28 777 888",
            website="https://rice.example.com",
            google_maps_url="https://maps.google.com/?cid=place-3",
        ),
        now,
    )
    repository.set_run_status(restaurant_run.id, RunStatus.COMPLETED, finished_at=now)

    restaurant_paths = campaign_exporter.export_campaign(restaurants.slug)
    restaurant_payload = json.loads(restaurant_paths.json_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in restaurant_payload] == ["Rice House"]


def test_campaign_deduplicates_vietnamese_aliases_across_district_runs(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")
    repository = SQLiteRepository(settings)
    repository.initialize()
    exporter = Exporter(repository, settings)
    campaign_exporter = CampaignExporter(repository, settings)
    now = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)

    campaign = repository.create_campaign("vietnam-dentists", "dentists", now)
    provider = FakeProvider(
        acquire_scripts=[
            ProviderScript(
                candidates=(
                    ProviderCandidate(
                        place_id="place-vi-1",
                        name="Nha Khoa Viet Smile",
                        category="Dentist",
                        address="1 Nguyen Hue, Quan 1",
                        phone="+84 28 111 222",
                        website="https://vietsmile.example.com",
                        google_maps_url="https://maps.google.com/?cid=place-vi-1",
                    ),
                ),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            ),
            ProviderScript(
                candidates=(
                    ProviderCandidate(
                        place_id="place-vi-1",
                        name="Nha Khoa Viet Smile",
                        category="Dental Clinic",
                        address="1 Nguyen Hue, Quan 1",
                        phone="+84 28 111 222",
                        website="https://vietsmile.example.com",
                        google_maps_url="https://maps.google.com/?cid=place-vi-1",
                    ),
                ),
                result=ProviderResult(
                    status="completed",
                    candidate_count=1,
                    rejected_row_count=0,
                    diagnostics_tail="done",
                ),
            ),
        ]
    )
    enricher = FakeEnricher(
        responses={
            "https://vietsmile.example.com": EnrichmentResult(
                status=EnrichmentStatus.COMPLETED,
                emails=("hello@vietsmile.example.com",),
            )
        }
    )
    service = MapsLeadService(repository, provider, enricher, exporter)

    first = service.scrape(
        "dentists",
        "Quan 1, Ho Chi Minh City",
        5,
        now,
        _progress_sink([]),
        campaign_slug=campaign.slug,
        query="nha khoa",
        language="vi",
    )
    second = service.scrape(
        "dentists",
        "Quan 3, Ho Chi Minh City",
        5,
        now,
        _progress_sink([]),
        campaign_slug=campaign.slug,
        query="phong kham nha khoa",
        language="vi",
    )

    assert first.run.status is RunStatus.COMPLETED
    assert second.run.status is RunStatus.COMPLETED
    assert [request.search_query for request in provider.acquire_requests] == [
        "nha khoa",
        "phong kham nha khoa",
    ]
    assert [request.language for request in provider.acquire_requests] == ["vi", "vi"]
    assert repository.remaining_quota(now) == DAILY_NEW_RECORD_LIMIT - 1

    export_paths = campaign_exporter.export_campaign(campaign.slug)
    payload = json.loads(export_paths.json_path.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["name"] == "Nha Khoa Viet Smile"
    assert payload[0]["discovered_in"] == [
        "Quan 1, Ho Chi Minh City",
        "Quan 3, Ho Chi Minh City",
    ]
    assert payload[0]["emails"] == ["hello@vietsmile.example.com"]
