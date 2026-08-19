from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mapslead.config import DAILY_NEW_RECORD_LIMIT, Settings
from mapslead.models import ProviderCandidate, ProviderRequest, RunRecord, RunStatus
from mapslead.normalize import build_identity, normalize_phone, normalize_text


def test_settings_use_fixed_daily_limit_and_timezone(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", export_dir=tmp_path / "exports")
    assert DAILY_NEW_RECORD_LIMIT == 1_000
    assert settings.timezone == ZoneInfo("Asia/Ho_Chi_Minh")


def test_place_id_is_preferred_identity() -> None:
    candidate = ProviderCandidate(name=" Example Dental ", place_id="ChIJ-123")
    assert build_identity(candidate).primary_key == "place:ChIJ-123"


@pytest.mark.parametrize("website", ["example.com", "www.example.com"])
def test_bare_domain_website_is_accepted_for_identity(website: str) -> None:
    candidate = ProviderCandidate(name="Example Dental", website=website)
    assert build_identity(candidate).primary_key == "name_domain:example dental|example.com"


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (
            ProviderCandidate(name="Example Dental", address=" 1 Main  Street "),
            "name_address:example dental|1 main street",
        ),
        (
            ProviderCandidate(name="Example Dental", phone="+84 (28) 123-456"),
            "name_phone:example dental|+8428123456",
        ),
        (
            ProviderCandidate(
                name="Example Dental",
                website="https://www.example.com/contact",
            ),
            "name_domain:example dental|example.com",
        ),
    ],
)
def test_fallback_identity_priority(candidate: ProviderCandidate, expected: str) -> None:
    assert build_identity(candidate).primary_key == expected


def test_candidate_without_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="name"):
        build_identity(ProviderCandidate(name=" ", place_id="ChIJ-123"))


def test_candidate_without_any_complete_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="identity"):
        build_identity(ProviderCandidate(name="Example Dental"))


def test_normalizers_collapse_unicode_text_and_phone() -> None:
    assert normalize_text("  EXAMPLE\u00a0  Dental  ") == "example dental"
    assert normalize_phone(" +84 (28) 123-456 ") == "+8428123456"


def test_provider_request_defaults_and_normalizes_query_language(tmp_path: Path) -> None:
    request = ProviderRequest(
        business="dentists",
        location="Hanoi",
        provider_dir=tmp_path / "provider",
        max_new_records=5,
        search_query="  phòng khám nha khoa  ",
        language="VI",
    )

    assert request.search_query == "phòng khám nha khoa"
    assert request.language == "vi"

    defaulted = ProviderRequest(
        business="dentists",
        location="Hanoi",
        provider_dir=tmp_path / "provider-default",
        max_new_records=5,
    )
    assert defaulted.search_query == "dentists"
    assert defaulted.language == "en"


@pytest.mark.parametrize(
    ("search_query", "language", "error_pattern"),
    [
        (" \n ", "en", "search query"),
        ("dentists\tteam", "en", "search query"),
        ("dentists", "english us", "language"),
        ("dentists", "vi\nVN", "language"),
    ],
)
def test_provider_request_rejects_invalid_query_or_language(
    tmp_path: Path,
    search_query: str,
    language: str,
    error_pattern: str,
) -> None:
    with pytest.raises(ValueError, match=error_pattern):
        ProviderRequest(
            business="dentists",
            location="Hanoi",
            provider_dir=tmp_path / "provider",
            max_new_records=5,
            search_query=search_query,
            language=language,
        )


def test_run_record_defaults_search_query_and_language() -> None:
    run = RunRecord(
        id="run-1",
        business_type="Dentists",
        location_query="Hanoi",
        requested_limit=5,
        status=RunStatus.RUNNING,
        started_at=datetime(2026, 8, 19, 10, 0, tzinfo=UTC),
        provider_dir=Path("/tmp/provider"),
    )

    assert run.search_query == "Dentists"
    assert run.language == "en"
