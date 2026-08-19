from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mapslead.config import Settings
from mapslead.errors import ExportError
from mapslead.models import CampaignSnapshot, EnrichmentStatus


@dataclass(slots=True)
class StubRepository:
    snapshots_by_campaign: dict[str, tuple[CampaignSnapshot, ...]]

    def campaign_snapshots(self, slug: str) -> tuple[CampaignSnapshot, ...]:
        return self.snapshots_by_campaign[slug]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")


@pytest.fixture
def campaign_slug() -> str:
    return "vietnam-dentists"


@pytest.fixture
def snapshots(campaign_slug: str) -> tuple[CampaignSnapshot, ...]:
    return (
        _snapshot(
            business_id=7,
            campaign_id=campaign_slug,
            name="Beta Dental",
            address="2 Main St",
            place_id="place-beta",
            category="Dentist",
            phone="+84 28 200 300",
            website="https://beta.example",
            rating=4.2,
            review_count=8,
            google_maps_url="https://maps.google.com/?cid=beta",
            emails=(),
            discovered_in=("Ho Chi Minh City",),
            enrichment_status=EnrichmentStatus.FAILED,
            enrichment_error="timeout",
        ),
        _snapshot(
            business_id=9,
            campaign_id=campaign_slug,
            name=" ALPHA DENTAL ",
            address=" 1 Main St ",
            place_id="place-alpha-2",
            category="Orthodontist",
            phone="+84 28 999 000",
            website="https://alpha-two.example",
            rating=4.8,
            review_count=19,
            google_maps_url="https://maps.google.com/?cid=alpha-two",
            emails=("c@example.com",),
            linkedin_url="https://linkedin.com/company/alpha-two",
            discovered_in=("Hanoi", "Ho Chi Minh City"),
            enrichment_status=EnrichmentStatus.PENDING,
            enrichment_error=None,
        ),
        _snapshot(
            business_id=4,
            campaign_id=campaign_slug,
            name="alpha dental",
            address="1 main st",
            place_id="place-alpha-1",
            category=None,
            phone=None,
            website=None,
            rating=None,
            review_count=None,
            google_maps_url=None,
            emails=("z@example.com", "a@example.com"),
            facebook_url="https://facebook.com/alpha",
            discovered_in=("Hanoi", "Ho Chi Minh City"),
            enrichment_status=EnrichmentStatus.COMPLETED,
            enrichment_error=None,
        ),
    )


def test_export_campaign_writes_sorted_csv_and_json_and_replaces_existing_pair(
    settings: Settings,
    campaign_slug: str,
    snapshots: tuple[CampaignSnapshot, ...],
) -> None:
    from mapslead.campaign_exporter import CAMPAIGN_CSV_FIELDS, CampaignExporter

    repository = StubRepository({campaign_slug: snapshots})
    exporter = CampaignExporter(repository, settings)
    campaign_dir = settings.export_dir / "campaigns" / campaign_slug
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "results.csv").write_text("old csv\n", encoding="utf-8")
    (campaign_dir / "results.json").write_text('{"old": true}\n', encoding="utf-8")

    paths = exporter.export_campaign(campaign_slug)

    with paths.csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))

    assert tuple(rows[0].keys()) == CAMPAIGN_CSV_FIELDS
    assert paths.csv_path == campaign_dir / "results.csv"
    assert paths.json_path == campaign_dir / "results.json"
    assert rows[0]["name"] == "alpha dental"
    assert rows[0]["emails"] == "a@example.com;z@example.com"
    assert rows[1]["discovered_in"] == "Hanoi;Ho Chi Minh City"
    assert payload[0]["campaign_id"] == campaign_slug
    assert payload[0]["emails"] == ["a@example.com", "z@example.com"]
    assert payload[1]["discovered_in"] == ["Hanoi", "Ho Chi Minh City"]
    assert payload[2]["enrichment_error"] == "timeout"
    assert payload[0]["website"] is None
    assert sorted(path.name for path in campaign_dir.iterdir()) == ["results.csv", "results.json"]


def test_export_campaign_preserves_existing_exports_when_json_serialization_fails(
    settings: Settings,
    campaign_slug: str,
    snapshots: tuple[CampaignSnapshot, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mapslead.campaign_exporter import CampaignExporter

    repository = StubRepository({campaign_slug: snapshots})
    exporter = CampaignExporter(repository, settings)
    campaign_dir = settings.export_dir / "campaigns" / campaign_slug
    campaign_dir.mkdir(parents=True, exist_ok=True)
    csv_path = campaign_dir / "results.csv"
    json_path = campaign_dir / "results.json"
    csv_path.write_text("stable csv\n", encoding="utf-8")
    json_path.write_text('{"stable": true}\n', encoding="utf-8")

    def fail_json_dumps(*args: object, **kwargs: object) -> str:
        raise TypeError("json exploded")

    monkeypatch.setattr("mapslead.campaign_exporter.json.dumps", fail_json_dumps)

    with pytest.raises(ExportError, match=campaign_slug):
        exporter.export_campaign(campaign_slug)

    assert csv_path.read_text(encoding="utf-8") == "stable csv\n"
    assert json_path.read_text(encoding="utf-8") == '{"stable": true}\n'
    assert sorted(path.name for path in campaign_dir.iterdir()) == ["results.csv", "results.json"]


def test_export_campaign_restores_existing_pair_when_second_replace_fails(
    settings: Settings,
    campaign_slug: str,
    snapshots: tuple[CampaignSnapshot, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mapslead.campaign_exporter import CampaignExporter

    repository = StubRepository({campaign_slug: snapshots})
    exporter = CampaignExporter(repository, settings)
    campaign_dir = settings.export_dir / "campaigns" / campaign_slug
    campaign_dir.mkdir(parents=True, exist_ok=True)
    csv_path = campaign_dir / "results.csv"
    json_path = campaign_dir / "results.json"
    csv_path.write_text("stable csv\n", encoding="utf-8")
    json_path.write_text('{"stable": true}\n', encoding="utf-8")

    real_replace = os.replace

    def fail_json_replace(src: str | Path, dst: str | Path) -> None:
        if Path(src).name == "results.json.tmp" and Path(dst).name == "results.json":
            raise OSError("replace exploded")
        real_replace(src, dst)

    monkeypatch.setattr("mapslead.campaign_exporter.os.replace", fail_json_replace)

    with pytest.raises(ExportError, match=campaign_slug):
        exporter.export_campaign(campaign_slug)

    assert csv_path.read_text(encoding="utf-8") == "stable csv\n"
    assert json_path.read_text(encoding="utf-8") == '{"stable": true}\n'
    assert sorted(path.name for path in campaign_dir.iterdir()) == ["results.csv", "results.json"]


@pytest.mark.parametrize(
    "unsafe_slug",
    (
        "../escape",
        "/absolute-campaign",
        "nested/run",
        "nested\\run",
        ".",
        "..",
    ),
)
def test_export_campaign_rejects_unsafe_slug(
    settings: Settings,
    snapshots: tuple[CampaignSnapshot, ...],
    unsafe_slug: str,
) -> None:
    from mapslead.campaign_exporter import CampaignExporter

    repository = StubRepository({unsafe_slug: snapshots})
    exporter = CampaignExporter(repository, settings)

    with pytest.raises((ExportError, Exception)):
        exporter.export_campaign(unsafe_slug)

    assert not settings.export_dir.exists()


def test_export_campaign_rejects_symlink_escape(
    settings: Settings,
    campaign_slug: str,
    snapshots: tuple[CampaignSnapshot, ...],
) -> None:
    from mapslead.campaign_exporter import CampaignExporter

    campaign_root = settings.export_dir / "campaigns"
    escaped_root = settings.export_dir.parent / "escaped-campaigns"
    escaped_root.mkdir(parents=True, exist_ok=True)
    campaign_root.parent.mkdir(parents=True, exist_ok=True)

    try:
        campaign_root.symlink_to(escaped_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    repository = StubRepository({campaign_slug: snapshots})
    exporter = CampaignExporter(repository, settings)

    with pytest.raises(ExportError, match="unsafe campaign slug"):
        exporter.export_campaign(campaign_slug)


def test_export_campaign_is_byte_identical_on_reexport(
    settings: Settings,
    campaign_slug: str,
    snapshots: tuple[CampaignSnapshot, ...],
) -> None:
    from mapslead.campaign_exporter import CampaignExporter

    repository = StubRepository({campaign_slug: snapshots})
    exporter = CampaignExporter(repository, settings)

    first = exporter.export_campaign(campaign_slug)
    first_csv = first.csv_path.read_bytes()
    first_json = first.json_path.read_bytes()
    second = exporter.export_campaign(campaign_slug)

    assert second.csv_path.read_bytes() == first_csv
    assert second.json_path.read_bytes() == first_json


def _snapshot(
    *,
    business_id: int,
    campaign_id: str,
    name: str,
    address: str | None,
    place_id: str | None,
    category: str | None,
    phone: str | None,
    website: str | None,
    rating: float | None,
    review_count: int | None,
    google_maps_url: str | None,
    emails: tuple[str, ...],
    discovered_in: tuple[str, ...],
    facebook_url: str | None = None,
    instagram_url: str | None = None,
    linkedin_url: str | None = None,
    x_url: str | None = None,
    youtube_url: str | None = None,
    enrichment_status: EnrichmentStatus,
    enrichment_error: str | None,
) -> CampaignSnapshot:
    timestamp = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    return CampaignSnapshot(
        business_id=business_id,
        campaign_id=campaign_id,
        discovered_in=discovered_in,
        name=name,
        business_type="dentists",
        first_seen_at=timestamp,
        last_seen_at=datetime(2026, 8, 19, 11, 30, tzinfo=UTC),
        place_id=place_id,
        category=category,
        address=address,
        phone=phone,
        website=website,
        rating=rating,
        review_count=review_count,
        google_maps_url=google_maps_url,
        emails=emails,
        facebook_url=facebook_url,
        instagram_url=instagram_url,
        linkedin_url=linkedin_url,
        x_url=x_url,
        youtube_url=youtube_url,
        enrichment_status=enrichment_status,
        enrichment_error=enrichment_error,
    )
