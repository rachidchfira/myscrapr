from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mapslead.config import DAILY_NEW_RECORD_LIMIT, Settings
from mapslead.models import ProviderCandidate
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
