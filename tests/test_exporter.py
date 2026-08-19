from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mapslead.config import Settings
from mapslead.errors import ExportError
from mapslead.exporter import Exporter
from mapslead.models import EnrichmentStatus, RunSnapshot


@dataclass(slots=True)
class StubRepository:
    snapshots_by_run: dict[str, tuple[RunSnapshot, ...]]

    def snapshots_for_run(self, run_id: str) -> tuple[RunSnapshot, ...]:
        return self.snapshots_by_run[run_id]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")


@pytest.fixture
def run_id() -> str:
    return "run-20260819"


@pytest.fixture
def snapshots(run_id: str) -> tuple[RunSnapshot, ...]:
    return (
        _snapshot(
            run_id=run_id,
            business_id=7,
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
            enrichment_status=EnrichmentStatus.FAILED,
            enrichment_error="timeout",
        ),
        _snapshot(
            run_id=run_id,
            business_id=9,
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
            enrichment_status=EnrichmentStatus.PENDING,
            enrichment_error=None,
        ),
        _snapshot(
            run_id=run_id,
            business_id=4,
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
            instagram_url=None,
            linkedin_url=None,
            x_url=None,
            youtube_url=None,
            enrichment_status=EnrichmentStatus.COMPLETED,
            enrichment_error=None,
        ),
    )


def test_export_run_writes_sorted_csv_and_json_and_replaces_existing_pair(
    settings: Settings,
    run_id: str,
    snapshots: tuple[RunSnapshot, ...],
) -> None:
    repository = StubRepository({run_id: snapshots})
    exporter = Exporter(repository, settings)
    run_dir = settings.export_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.csv").write_text("old csv\n", encoding="utf-8")
    (run_dir / "results.json").write_text('{"old": true}\n', encoding="utf-8")

    paths = exporter.export_run(run_id)

    expected_csv = (
        "place_id,name,category,address,phone,website,rating,reviews_count,google_maps_url,emails,facebook,instagram,linkedin,x,youtube,first_seen_at,last_seen_at,run_id\n"
        "place-alpha-1,alpha dental,,1 main st,,,,,,a@example.com;z@example.com,https://facebook.com/alpha,,,,,2026-08-19T10:00:00+00:00,2026-08-19T11:30:00+00:00,run-20260819\n"
        "place-alpha-2, ALPHA DENTAL ,Orthodontist, 1 Main St ,+84 28 999 000,https://alpha-two.example,4.8,19,https://maps.google.com/?cid=alpha-two,c@example.com,,,https://linkedin.com/company/alpha-two,,,2026-08-19T10:00:00+00:00,2026-08-19T11:30:00+00:00,run-20260819\n"
        "place-beta,Beta Dental,Dentist,2 Main St,+84 28 200 300,https://beta.example,4.2,8,https://maps.google.com/?cid=beta,,,,,,,2026-08-19T10:00:00+00:00,2026-08-19T11:30:00+00:00,run-20260819\n"
    )
    expected_json = [
        {
            "place_id": "place-alpha-1",
            "name": "alpha dental",
            "category": None,
            "address": "1 main st",
            "phone": None,
            "website": None,
            "rating": None,
            "reviews_count": None,
            "google_maps_url": None,
            "emails": ["a@example.com", "z@example.com"],
            "facebook": "https://facebook.com/alpha",
            "instagram": None,
            "linkedin": None,
            "x": None,
            "youtube": None,
            "first_seen_at": "2026-08-19T10:00:00+00:00",
            "last_seen_at": "2026-08-19T11:30:00+00:00",
            "run_id": "run-20260819",
        },
        {
            "place_id": "place-alpha-2",
            "name": " ALPHA DENTAL ",
            "category": "Orthodontist",
            "address": " 1 Main St ",
            "phone": "+84 28 999 000",
            "website": "https://alpha-two.example",
            "rating": 4.8,
            "reviews_count": 19,
            "google_maps_url": "https://maps.google.com/?cid=alpha-two",
            "emails": ["c@example.com"],
            "facebook": None,
            "instagram": None,
            "linkedin": "https://linkedin.com/company/alpha-two",
            "x": None,
            "youtube": None,
            "first_seen_at": "2026-08-19T10:00:00+00:00",
            "last_seen_at": "2026-08-19T11:30:00+00:00",
            "run_id": "run-20260819",
        },
        {
            "place_id": "place-beta",
            "name": "Beta Dental",
            "category": "Dentist",
            "address": "2 Main St",
            "phone": "+84 28 200 300",
            "website": "https://beta.example",
            "rating": 4.2,
            "reviews_count": 8,
            "google_maps_url": "https://maps.google.com/?cid=beta",
            "emails": [],
            "facebook": None,
            "instagram": None,
            "linkedin": None,
            "x": None,
            "youtube": None,
            "first_seen_at": "2026-08-19T10:00:00+00:00",
            "last_seen_at": "2026-08-19T11:30:00+00:00",
            "run_id": "run-20260819",
        },
    ]

    assert paths.csv_path == run_dir / "results.csv"
    assert paths.json_path == run_dir / "results.json"
    assert paths.csv_path.read_text(encoding="utf-8") == expected_csv
    assert json.loads(paths.json_path.read_text(encoding="utf-8")) == expected_json
    assert sorted(path.name for path in run_dir.iterdir()) == ["results.csv", "results.json"]


def test_export_run_preserves_existing_exports_when_json_serialization_fails(
    settings: Settings,
    run_id: str,
    snapshots: tuple[RunSnapshot, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = StubRepository({run_id: snapshots})
    exporter = Exporter(repository, settings)
    run_dir = settings.export_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "results.csv"
    json_path = run_dir / "results.json"
    csv_path.write_text("stable csv\n", encoding="utf-8")
    json_path.write_text('{"stable": true}\n', encoding="utf-8")

    def fail_json_dumps(*args: object, **kwargs: object) -> str:
        raise TypeError("json exploded")

    monkeypatch.setattr("mapslead.exporter.json.dumps", fail_json_dumps)

    with pytest.raises(ExportError, match="run-20260819"):
        exporter.export_run(run_id)

    assert csv_path.read_text(encoding="utf-8") == "stable csv\n"
    assert json_path.read_text(encoding="utf-8") == '{"stable": true}\n'
    assert sorted(path.name for path in run_dir.iterdir()) == ["results.csv", "results.json"]


def test_export_run_restores_existing_pair_when_second_replace_fails(
    settings: Settings,
    run_id: str,
    snapshots: tuple[RunSnapshot, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = StubRepository({run_id: snapshots})
    exporter = Exporter(repository, settings)
    run_dir = settings.export_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "results.csv"
    json_path = run_dir / "results.json"
    csv_path.write_text("stable csv\n", encoding="utf-8")
    json_path.write_text('{"stable": true}\n', encoding="utf-8")

    real_replace = __import__("os").replace

    def fail_json_replace(src: str | Path, dst: str | Path) -> None:
        if Path(src).name == "results.json.tmp" and Path(dst).name == "results.json":
            raise OSError("replace exploded")
        real_replace(src, dst)

    monkeypatch.setattr("mapslead.exporter.os.replace", fail_json_replace)

    with pytest.raises(ExportError, match="run-20260819"):
        exporter.export_run(run_id)

    assert csv_path.read_text(encoding="utf-8") == "stable csv\n"
    assert json_path.read_text(encoding="utf-8") == '{"stable": true}\n'
    assert sorted(path.name for path in run_dir.iterdir()) == ["results.csv", "results.json"]


@pytest.mark.parametrize(
    "unsafe_run_id",
    (
        "../escape",
        "/absolute-run",
        "nested/run",
        "nested\\run",
        ".",
        "..",
    ),
)
def test_export_run_rejects_unsafe_run_id(
    settings: Settings,
    snapshots: tuple[RunSnapshot, ...],
    unsafe_run_id: str,
) -> None:
    repository = StubRepository({unsafe_run_id: snapshots})
    exporter = Exporter(repository, settings)

    with pytest.raises(ExportError, match="unsafe run_id"):
        exporter.export_run(unsafe_run_id)

    assert not settings.export_dir.exists()


def _snapshot(
    *,
    run_id: str,
    business_id: int,
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
    facebook_url: str | None = None,
    instagram_url: str | None = None,
    linkedin_url: str | None = None,
    x_url: str | None = None,
    youtube_url: str | None = None,
    enrichment_status: EnrichmentStatus,
    enrichment_error: str | None,
) -> RunSnapshot:
    timestamp = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    return RunSnapshot(
        business_id=business_id,
        run_id=run_id,
        name=name,
        business_type="dentists",
        location_query="HCMC",
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
