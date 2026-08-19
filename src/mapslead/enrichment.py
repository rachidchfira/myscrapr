from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import SplitResult, parse_qs, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import urlopen

from mapslead.errors import UnsafeUrlError
from mapslead.models import EnrichmentResult, EnrichmentStatus
from mapslead.normalize import registrable_domain
from mapslead.ports import Clock, PageFetcher

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_DOWNLOAD_EXTENSIONS = frozenset(
    {
        ".7z",
        ".csv",
        ".doc",
        ".docx",
        ".exe",
        ".gif",
        ".gz",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".rar",
        ".svg",
        ".tar",
        ".tgz",
        ".webp",
        ".xls",
        ".xlsx",
        ".zip",
    }
)
_AUTH_PATH_MARKERS = frozenset({"account", "login", "signin"})


class Resolver(Protocol):
    def resolve(self, hostname: str) -> tuple[str, ...]: ...


class RobotsChecker(Protocol):
    def allows(self, url: str) -> tuple[bool, str | None]: ...


@dataclass(frozen=True, slots=True)
class FetchedPage:
    final_url: str
    html: str


class UrlPolicy:
    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver or _SocketResolver()

    def validate(self, url: str, allowed_registrable_domain: str | None = None) -> str:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeUrlError(f"unsupported URL scheme: {url}")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeUrlError(f"URL credentials are not allowed: {url}")
        hostname = parsed.hostname
        if hostname is None:
            raise UnsafeUrlError(f"URL hostname is required: {url}")

        normalized_url = _rebuild_url(parsed)
        normalized_hostname = hostname.casefold().rstrip(".")
        self._ensure_globally_routable(normalized_hostname)

        if allowed_registrable_domain is not None:
            candidate_domain = registrable_domain(normalized_url)
            if candidate_domain != allowed_registrable_domain:
                raise UnsafeUrlError(
                    "redirect changed registrable domain: "
                    f"{candidate_domain!r} != {allowed_registrable_domain!r}"
                )

        return normalized_url

    def _ensure_globally_routable(self, hostname: str) -> None:
        literal_ip = _parse_ip(hostname)
        if literal_ip is not None:
            if not literal_ip.is_global:
                raise UnsafeUrlError(f"hostname resolves to a non-global address: {hostname}")
            return

        try:
            addresses = self._resolver.resolve(hostname)
        except OSError as exc:
            raise UnsafeUrlError(f"failed to resolve hostname {hostname}: {exc}") from exc

        if not addresses:
            raise UnsafeUrlError(f"hostname did not resolve to any address: {hostname}")

        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global:
                raise UnsafeUrlError(f"hostname resolves to a non-global address: {hostname}")


class ScraplingPageFetcher:
    def fetch(self, url: str) -> FetchedPage:
        from scrapling.fetchers import Fetcher

        page = Fetcher.get(url)
        return FetchedPage(final_url=str(page.url), html=str(page.html_content))


class WebsiteEnrichmentService:
    def __init__(
        self,
        *,
        page_fetcher: PageFetcher,
        url_policy: UrlPolicy | None = None,
        robots_checker: RobotsChecker | None = None,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._page_fetcher = page_fetcher
        self._url_policy = url_policy or UrlPolicy()
        self._robots_checker = robots_checker or _RobotFileRobotsChecker()
        self._clock = clock or _SystemClock()
        self._sleeper = sleeper
        self._last_request_by_domain: dict[str, float] = {}

    def enrich(self, website: str) -> EnrichmentResult:
        warnings: list[str] = []
        emails: dict[str, None] = {}
        socials = _SocialProfiles()

        try:
            root_url = _normalize_website(website)
            validated_root = self._url_policy.validate(root_url)
            allowed_domain = registrable_domain(validated_root)
            if allowed_domain is None:
                raise UnsafeUrlError(f"website must use a registrable domain: {website}")

            root_page = self._fetch_page(validated_root, allowed_domain, warnings)
            if root_page is None:
                return self._build_completed_result(emails, socials, warnings)

            _extract_contact_data(root_page.html, emails, socials)
            for page_url in _discover_candidate_pages(
                root_page.final_url,
                root_page.html,
                allowed_domain,
            ):
                page = self._fetch_page(page_url, allowed_domain, warnings)
                if page is None:
                    continue
                _extract_contact_data(page.html, emails, socials)

            return self._build_completed_result(emails, socials, warnings)
        except (OSError, RuntimeError, UnsafeUrlError, ValueError) as exc:
            messages = [*warnings, str(exc)]
            return EnrichmentResult(
                status=EnrichmentStatus.FAILED,
                emails=tuple(emails),
                facebook_url=socials.facebook_url,
                instagram_url=socials.instagram_url,
                linkedin_url=socials.linkedin_url,
                x_url=socials.x_url,
                youtube_url=socials.youtube_url,
                error=_combine_messages(messages),
            )

    def _fetch_page(
        self,
        url: str,
        allowed_domain: str,
        warnings: list[str],
    ) -> FetchedPage | None:
        validated_url = self._url_policy.validate(url, allowed_registrable_domain=allowed_domain)
        allowed, warning = self._robots_checker.allows(validated_url)
        if warning is not None and warning not in warnings:
            warnings.append(warning)
        if not allowed:
            return None

        self._wait_for_domain_slot(validated_url)
        page = self._page_fetcher.fetch(validated_url)
        final_url = self._url_policy.validate(
            page.final_url,
            allowed_registrable_domain=allowed_domain,
        )
        return FetchedPage(final_url=final_url, html=page.html)

    def _wait_for_domain_slot(self, url: str) -> None:
        domain_key = registrable_domain(url)
        if domain_key is None:
            hostname = urlsplit(url).hostname
            if hostname is None:
                raise UnsafeUrlError(f"URL hostname is required: {url}")
            domain_key = hostname.casefold().rstrip(".")

        now = self._clock.monotonic()
        last = self._last_request_by_domain.get(domain_key)
        if last is not None:
            elapsed = now - last
            if elapsed < 2.0:
                self._sleeper(2.0 - elapsed)
        self._last_request_by_domain[domain_key] = self._clock.monotonic()

    def _build_completed_result(
        self,
        emails: dict[str, None],
        socials: _SocialProfiles,
        warnings: Sequence[str],
    ) -> EnrichmentResult:
        return EnrichmentResult(
            status=EnrichmentStatus.COMPLETED,
            emails=tuple(emails),
            facebook_url=socials.facebook_url,
            instagram_url=socials.instagram_url,
            linkedin_url=socials.linkedin_url,
            x_url=socials.x_url,
            youtube_url=socials.youtube_url,
            error=_combine_messages(warnings),
        )


@dataclass(slots=True)
class _ScoredLink:
    priority: int
    index: int
    url: str


@dataclass(slots=True)
class _SocialProfiles:
    facebook_url: str | None = None
    instagram_url: str | None = None
    linkedin_url: str | None = None
    x_url: str | None = None
    youtube_url: str | None = None

    def set_if_empty(self, network: str, url: str) -> None:
        if network == "facebook" and self.facebook_url is None:
            self.facebook_url = url
        elif network == "instagram" and self.instagram_url is None:
            self.instagram_url = url
        elif network == "linkedin" and self.linkedin_url is None:
            self.linkedin_url = url
        elif network == "x" and self.x_url is None:
            self.x_url = url
        elif network == "youtube" and self.youtube_url is None:
            self.youtube_url = url


class _SocketResolver:
    def resolve(self, hostname: str) -> tuple[str, ...]:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        addresses: set[str] = set()
        for _family, _socktype, _proto, _canonname, sockaddr in infos:
            address = sockaddr[0]
            if isinstance(address, str):
                addresses.add(address)
        return tuple(sorted(addresses))


class _RobotFileRobotsChecker:
    def __init__(self, user_agent: str = "*") -> None:
        self._user_agent = user_agent
        self._cache: dict[str, tuple[Any | None, str | None]] = {}

    def allows(self, url: str) -> tuple[bool, str | None]:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        parser, warning = self._cache.get(origin, (None, None))
        if origin not in self._cache:
            parser, warning = self._load_parser(origin)
            self._cache[origin] = (parser, warning)
        if parser is None:
            return True, warning
        return bool(parser.can_fetch(self._user_agent, url)), warning

    def _load_parser(self, origin: str) -> tuple[Any | None, str | None]:
        from urllib.robotparser import RobotFileParser

        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser(robots_url)
        try:
            with urlopen(robots_url) as response:
                body = response.read().decode("utf-8", errors="replace")
        except OSError as exc:
            warning = f"robots unavailable for {robots_url}: {exc}"
            return None, warning
        parser.parse(body.splitlines())
        return parser, None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text_fragments: list[str] = []
        self._skip_depth = 0
        self._current_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if tag != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href is not None:
            self._current_href = href
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if tag == "a" and self._current_href is not None:
            self.links.append((self._current_href, "".join(self._anchor_text).strip()))
            self._current_href = None
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if data.strip():
            self.text_fragments.append(data)
        if self._current_href is not None:
            self._anchor_text.append(data)


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


def _normalize_website(website: str) -> str:
    trimmed = website.strip()
    if "://" not in trimmed:
        return f"https://{trimmed}"
    return trimmed


def _discover_candidate_pages(
    current_url: str,
    html: str,
    allowed_domain: str,
) -> tuple[str, ...]:
    parser = _PageParser()
    parser.feed(html)

    scored: list[_ScoredLink] = []
    seen: set[str] = {current_url}
    for index, (href, text) in enumerate(parser.links):
        candidate_url = _canonical_page_url(urljoin(current_url, href))
        if candidate_url in seen:
            continue

        candidate_domain = registrable_domain(candidate_url)
        if candidate_domain != allowed_domain:
            continue

        parsed = urlsplit(candidate_url)
        if _is_filtered_path(parsed.path):
            continue

        priority = _link_priority(text, parsed.path)
        seen.add(candidate_url)
        scored.append(_ScoredLink(priority=priority, index=index, url=candidate_url))

    scored.sort(key=lambda item: (item.priority, item.index, item.url))
    return tuple(item.url for item in scored[:3])


def _extract_contact_data(html: str, emails: dict[str, None], socials: _SocialProfiles) -> None:
    parser = _PageParser()
    parser.feed(html)

    for fragment in parser.text_fragments:
        for match in _EMAIL_RE.findall(fragment):
            emails.setdefault(match.casefold(), None)

    for href, _text in parser.links:
        if href.casefold().startswith("mailto:"):
            address = _normalize_mailto(href)
            if address is not None:
                emails.setdefault(address, None)
            continue
        social = _normalize_social_url(href)
        if social is not None:
            network, normalized_url = social
            socials.set_if_empty(network, normalized_url)


def _normalize_mailto(href: str) -> str | None:
    address = href.split(":", 1)[1]
    mailbox = unquote(address).split("?", 1)[0].strip().casefold()
    if not mailbox or "@" not in mailbox:
        return None
    return mailbox


def _normalize_social_url(href: str) -> tuple[str, str] | None:
    parsed = urlsplit(href)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or hostname is None:
        return None

    host = hostname.casefold()
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return None

    if host in {"facebook.com", "m.facebook.com", "www.facebook.com"}:
        handle = segments[0]
        if handle in {"pages", "sharer", "share.php"}:
            return None
        return "facebook", f"https://www.facebook.com/{handle}"

    if host in {"instagram.com", "www.instagram.com"}:
        handle = segments[0]
        return "instagram", f"https://www.instagram.com/{handle}"

    if host in {"linkedin.com", "www.linkedin.com"} and len(segments) >= 2:
        section = segments[0]
        if section not in {"company", "in", "school"}:
            return None
        return "linkedin", f"https://www.linkedin.com/{section}/{segments[1]}"

    if host in {"twitter.com", "www.twitter.com", "x.com", "www.x.com"}:
        handle = segments[0]
        if handle in {"home", "intent", "search", "share"}:
            return None
        return "x", f"https://x.com/{handle}"

    if host in {"youtube.com", "www.youtube.com"}:
        return _normalize_youtube(parsed)

    if host == "youtu.be":
        video_id = segments[0]
        if video_id:
            return "youtube", f"https://www.youtube.com/watch?v={video_id}"
    return None


def _normalize_youtube(parsed: SplitResult) -> tuple[str, str] | None:
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return None
    first = segments[0]
    if first.startswith("@"):
        return "youtube", f"https://www.youtube.com/{first}"
    if first in {"channel", "c", "user"} and len(segments) >= 2:
        return "youtube", f"https://www.youtube.com/{first}/{segments[1]}"
    if first == "watch":
        query = parse_qs(parsed.query)
        video_id = query.get("v", [None])[0]
        if video_id is None:
            return None
        return "youtube", f"https://www.youtube.com/watch?v={video_id}"
    return None


def _canonical_page_url(url: str) -> str:
    parsed = urlsplit(url)
    cleaned_path = parsed.path or ""
    if cleaned_path not in {"", "/"}:
        cleaned_path = cleaned_path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), cleaned_path, "", ""))


def _is_filtered_path(path: str) -> bool:
    lowered = path.casefold()
    if any(lowered.endswith(extension) for extension in _DOWNLOAD_EXTENSIONS):
        return True
    segments = {segment for segment in lowered.split("/") if segment}
    return not segments.isdisjoint(_AUTH_PATH_MARKERS)


def _link_priority(text: str, path: str) -> int:
    combined = f"{text} {path}".casefold()
    if "contact" in combined:
        return 0
    if "about" in combined:
        return 1
    if "team" in combined:
        return 2
    return 3


def _combine_messages(messages: Sequence[str | None]) -> str | None:
    filtered = [message for message in messages if message]
    if not filtered:
        return None
    return "; ".join(filtered)


def _parse_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _rebuild_url(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("URL hostname is required")
    normalized_hostname = hostname.casefold().rstrip(".")
    if ":" in normalized_hostname and not normalized_hostname.startswith("["):
        normalized_hostname = f"[{normalized_hostname}]"

    port = parsed.port
    if port is None:
        netloc = normalized_hostname
    else:
        netloc = f"{normalized_hostname}:{port}"
    rebuilt = urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "", parsed.query, ""))
    return str(rebuilt)
