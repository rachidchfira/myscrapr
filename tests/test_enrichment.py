from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mapslead.enrichment import FetchedPage, UrlPolicy, WebsiteEnrichmentService
from mapslead.errors import UnsafeUrlError
from mapslead.models import EnrichmentStatus


@dataclass
class FakeFetcher:
    pages: dict[str, FetchedPage | Exception]
    requested_urls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> FetchedPage:
        self.requested_urls.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


@dataclass
class FakeResolver:
    addresses_by_host: dict[str, tuple[str, ...]]

    def resolve(self, hostname: str) -> tuple[str, ...]:
        return self.addresses_by_host[hostname]


@dataclass
class FakeRobotsChecker:
    decisions: dict[str, tuple[bool, str | None]] = field(default_factory=dict)
    checked_urls: list[str] = field(default_factory=list)

    def allows(self, url: str) -> tuple[bool, str | None]:
        self.checked_urls.append(url)
        return self.decisions.get(url, (True, None))


@dataclass
class FakeClock:
    value: float = 0.0

    def now(self) -> datetime:
        return datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.value


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "web"


@pytest.fixture
def home_html(fixture_dir: Path) -> str:
    return (fixture_dir / "home.html").read_text(encoding="utf-8")


@pytest.fixture
def contact_html(fixture_dir: Path) -> str:
    return (fixture_dir / "contact.html").read_text(encoding="utf-8")


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver(
        addresses_by_host={
            "example.com": ("93.184.216.34",),
            "blog.example.com": ("93.184.216.34",),
            "www.example.com": ("93.184.216.34",),
            "evil.example": ("93.184.216.35",),
            "mixed.example": ("93.184.216.34", "10.0.0.5"),
        }
    )


@pytest.fixture
def policy(resolver: FakeResolver) -> UrlPolicy:
    return UrlPolicy(resolver=resolver)


def build_service(
    pages: dict[str, FetchedPage | Exception],
    *,
    policy: UrlPolicy,
    robots_checker: FakeRobotsChecker | None = None,
    clock: FakeClock | None = None,
    sleep_calls: list[float] | None = None,
) -> tuple[WebsiteEnrichmentService, FakeFetcher, FakeClock, list[float]]:
    fetcher = FakeFetcher(pages=pages)
    robots = robots_checker or FakeRobotsChecker()
    active_clock = clock or FakeClock()
    sleeps = sleep_calls if sleep_calls is not None else []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        active_clock.value += seconds

    return (
        WebsiteEnrichmentService(
            page_fetcher=fetcher,
            url_policy=policy,
            robots_checker=robots,
            clock=active_clock,
            sleeper=sleeper,
        ),
        fetcher,
        active_clock,
        sleeps,
    )


def test_enrichment_prefers_contact_then_about_then_team_and_caps_four_pages(
    policy: UrlPolicy,
    home_html: str,
    contact_html: str,
) -> None:
    service, fetcher, _, _ = build_service(
        {
            "https://example.com": FetchedPage(
                final_url="https://example.com",
                html=home_html,
            ),
            "https://example.com/contact": FetchedPage(
                final_url="https://example.com/contact",
                html=contact_html,
            ),
            "https://example.com/about": FetchedPage(
                final_url="https://example.com/about",
                html="<p>About Example Dental</p>",
            ),
            "https://example.com/team": FetchedPage(
                final_url="https://example.com/team",
                html="<p>Meet the team</p>",
            ),
        },
        policy=policy,
    )

    result = service.enrich("https://example.com")

    assert fetcher.requested_urls == [
        "https://example.com",
        "https://example.com/contact",
        "https://example.com/about",
        "https://example.com/team",
    ]
    assert result.status == EnrichmentStatus.COMPLETED
    assert result.emails == ("hello@example.com", "sales@example.com")
    assert result.facebook_url == "https://www.facebook.com/ExampleDental"
    assert result.instagram_url == "https://www.instagram.com/exampledental"
    assert result.linkedin_url == "https://www.linkedin.com/company/example-dental"
    assert result.x_url == "https://x.com/exampledental"
    assert result.youtube_url == "https://www.youtube.com/@ExampleDental"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/private",
        "https://user:password@example.com",
        "http://[fd00::1]/admin",
        "http://[fe80::1]/admin",
    ],
)
def test_url_policy_rejects_unsafe_destinations(url: str, policy: UrlPolicy) -> None:
    with pytest.raises(UnsafeUrlError):
        policy.validate(url)


def test_url_policy_rejects_mixed_public_and_private_resolution(policy: UrlPolicy) -> None:
    with pytest.raises(UnsafeUrlError, match="mixed.example"):
        policy.validate("https://mixed.example/contact")


@pytest.mark.parametrize(
    "final_url",
    [
        "https://evil.example",
        "http://127.0.0.1/admin",
    ],
)
def test_enrichment_revalidates_redirect_targets_and_contains_failure(
    policy: UrlPolicy,
    final_url: str,
) -> None:
    service, _, _, _ = build_service(
        {
            "https://example.com": FetchedPage(final_url=final_url, html="<p>redirected</p>"),
        },
        policy=policy,
    )

    result = service.enrich("https://example.com")

    assert result.status == EnrichmentStatus.FAILED
    assert result.error is not None


def test_enrichment_discovers_same_registrable_domain_pages_only(
    policy: UrlPolicy,
) -> None:
    service, fetcher, _, _ = build_service(
        {
            "https://example.com": FetchedPage(
                final_url="https://example.com",
                html=(
                    '<a href="https://blog.example.com/contact">Contact us</a>'
                    '<a href="https://evil.example/contact">Ignore</a>'
                ),
            ),
            "https://blog.example.com/contact": FetchedPage(
                final_url="https://blog.example.com/contact",
                html="<p>hello@example.com</p>",
            ),
        },
        policy=policy,
    )

    result = service.enrich("https://example.com")

    assert result.status == EnrichmentStatus.COMPLETED
    assert fetcher.requested_urls == [
        "https://example.com",
        "https://blog.example.com/contact",
    ]
    assert result.emails == ("hello@example.com",)


def test_enrichment_skips_robots_disallowed_and_non_page_links(
    policy: UrlPolicy,
) -> None:
    robots = FakeRobotsChecker(
        decisions={
            "https://example.com/team": (False, None),
        }
    )
    service, fetcher, _, _ = build_service(
        {
            "https://example.com": FetchedPage(
                final_url="https://example.com",
                html=(
                    '<a href="/contact">Contact</a>'
                    '<a href="/team">Team</a>'
                    '<a href="/login">Login</a>'
                    '<a href="/brochure.pdf">PDF</a>'
                ),
            ),
            "https://example.com/contact": FetchedPage(
                final_url="https://example.com/contact",
                html="<p>hello@example.com</p>",
            ),
        },
        policy=policy,
        robots_checker=robots,
    )

    result = service.enrich("https://example.com")

    assert result.status == EnrichmentStatus.COMPLETED
    assert fetcher.requested_urls == [
        "https://example.com",
        "https://example.com/contact",
    ]


def test_enrichment_records_warning_when_robots_lookup_fails(
    policy: UrlPolicy,
) -> None:
    robots = FakeRobotsChecker(
        decisions={
            "https://example.com": (True, "robots unavailable for https://example.com/robots.txt"),
        }
    )
    service, _, _, _ = build_service(
        {
            "https://example.com": FetchedPage(
                final_url="https://example.com",
                html="<p>hello@example.com</p>",
            ),
        },
        policy=policy,
        robots_checker=robots,
    )

    result = service.enrich("https://example.com")

    assert result.status == EnrichmentStatus.COMPLETED
    assert result.error == "robots unavailable for https://example.com/robots.txt"
    assert result.emails == ("hello@example.com",)


def test_enrichment_enforces_two_second_spacing_per_registrable_domain(
    policy: UrlPolicy,
    home_html: str,
    contact_html: str,
) -> None:
    sleep_calls: list[float] = []
    service, fetcher, clock, _ = build_service(
        {
            "https://example.com": FetchedPage(final_url="https://example.com", html=home_html),
            "https://example.com/contact": FetchedPage(
                final_url="https://example.com/contact",
                html=contact_html,
            ),
            "https://example.com/about": FetchedPage(
                final_url="https://example.com/about",
                html="<p>About</p>",
            ),
            "https://example.com/team": FetchedPage(
                final_url="https://example.com/team",
                html="<p>Team</p>",
            ),
        },
        policy=policy,
        clock=FakeClock(),
        sleep_calls=sleep_calls,
    )

    result = service.enrich("https://example.com")

    assert result.status == EnrichmentStatus.COMPLETED
    assert fetcher.requested_urls == [
        "https://example.com",
        "https://example.com/contact",
        "https://example.com/about",
        "https://example.com/team",
    ]
    assert sleep_calls == [2.0, 2.0, 2.0]
    assert clock.monotonic() == 6.0


def test_enrichment_fetch_failure_returns_failed_result_instead_of_raising(
    policy: UrlPolicy,
) -> None:
    service, fetcher, _, _ = build_service(
        {
            "https://example.com": FetchedPage(
                final_url="https://example.com",
                html='<a href="/contact">Contact</a><a href="/about">About</a>',
            ),
            "https://example.com/contact": FetchedPage(
                final_url="https://example.com/contact",
                html="<p>hello@example.com</p>",
            ),
            "https://example.com/about": RuntimeError("boom"),
        },
        policy=policy,
    )

    result = service.enrich("https://example.com")

    assert fetcher.requested_urls == [
        "https://example.com",
        "https://example.com/contact",
        "https://example.com/about",
    ]
    assert result.status == EnrichmentStatus.FAILED
    assert result.error is not None
    assert "boom" in result.error


def test_enrichment_accepts_bare_domains_by_normalizing_to_https(policy: UrlPolicy) -> None:
    service, fetcher, _, _ = build_service(
        {
            "https://example.com": FetchedPage(
                final_url="https://example.com",
                html="<p>hello@example.com</p>",
            ),
        },
        policy=policy,
    )

    result = service.enrich("example.com")

    assert result.status == EnrichmentStatus.COMPLETED
    assert fetcher.requested_urls == ["https://example.com"]
    assert result.emails == ("hello@example.com",)
