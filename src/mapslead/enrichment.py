from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import SplitResult, parse_qs, unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

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
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class Resolver(Protocol):
    def resolve(self, hostname: str) -> tuple[str, ...]: ...


class Requester(Protocol):
    def fetch(self, request: TransportRequest) -> TransportResponse: ...


class RobotsChecker(Protocol):
    def allows(self, url: str) -> tuple[bool, str | None]: ...


@dataclass(frozen=True, slots=True)
class FetchedPage:
    final_url: str
    html: str


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    normalized_url: str
    scheme: str
    hostname: str
    port: int
    target: str
    registrable_domain: str | None
    resolved_addresses: tuple[str, ...]
    host_header: str


@dataclass(frozen=True, slots=True)
class TransportRequest:
    url: str
    scheme: str
    hostname: str
    port: int
    target: str
    host_header: str
    ip_address: str


@dataclass(frozen=True, slots=True)
class TransportResponse:
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    peer_ip: str


class UrlPolicy:
    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver or _SocketResolver()

    def validate(self, url: str, allowed_registrable_domain: str | None = None) -> str:
        return self.inspect(url, allowed_registrable_domain=allowed_registrable_domain).normalized_url

    def inspect(self, url: str, allowed_registrable_domain: str | None = None) -> ValidatedUrl:
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
        resolved_addresses = self._ensure_globally_routable(normalized_hostname)
        domain = registrable_domain(normalized_url)

        if allowed_registrable_domain is not None and domain != allowed_registrable_domain:
            raise UnsafeUrlError(
                "redirect changed registrable domain: "
                f"{domain!r} != {allowed_registrable_domain!r}"
            )

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host_header = normalized_hostname
        if (parsed.scheme == "https" and port != 443) or (parsed.scheme == "http" and port != 80):
            host_header = f"{host_header}:{port}"

        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        return ValidatedUrl(
            normalized_url=normalized_url,
            scheme=parsed.scheme.casefold(),
            hostname=normalized_hostname,
            port=port,
            target=target,
            registrable_domain=domain,
            resolved_addresses=resolved_addresses,
            host_header=host_header,
        )

    def _ensure_globally_routable(self, hostname: str) -> tuple[str, ...]:
        literal_ip = _parse_ip(hostname)
        if literal_ip is not None:
            if not literal_ip.is_global:
                raise UnsafeUrlError(f"hostname resolves to a non-global address: {hostname}")
            return (literal_ip.compressed,)

        try:
            addresses = self._resolver.resolve(hostname)
        except OSError as exc:
            raise UnsafeUrlError(f"failed to resolve hostname {hostname}: {exc}") from exc

        if not addresses:
            raise UnsafeUrlError(f"hostname did not resolve to any address: {hostname}")

        normalized_addresses: list[str] = []
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global:
                raise UnsafeUrlError(f"hostname resolves to a non-global address: {hostname}")
            normalized_addresses.append(parsed.compressed)
        return tuple(normalized_addresses)


class SafeHttpTransport:
    def __init__(
        self,
        *,
        url_policy: UrlPolicy | None = None,
        requester: Requester | None = None,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_redirects: int = 5,
    ) -> None:
        self._url_policy = url_policy or UrlPolicy()
        self._requester = requester or _SocketRequester()
        self._clock = clock or _SystemClock()
        self._sleeper = sleeper
        self._max_redirects = max_redirects
        self._last_request_by_domain: dict[str, float] = {}

    def get(self, url: str, *, allowed_registrable_domain: str | None = None) -> TransportResponse:
        current_url = url
        for _ in range(self._max_redirects + 1):
            validated = self._url_policy.inspect(
                current_url,
                allowed_registrable_domain=allowed_registrable_domain,
            )
            response = self._fetch_once(validated)
            if response.status_code in _REDIRECT_STATUSES:
                location = _header_value(response.headers, "Location")
                if location is None:
                    return TransportResponse(
                        final_url=validated.normalized_url,
                        status_code=response.status_code,
                        headers=response.headers,
                        body=response.body,
                        peer_ip=response.peer_ip,
                    )
                current_url = urljoin(validated.normalized_url, location)
                continue

            return TransportResponse(
                final_url=validated.normalized_url,
                status_code=response.status_code,
                headers=response.headers,
                body=response.body,
                peer_ip=response.peer_ip,
            )

        raise UnsafeUrlError(f"too many redirects while fetching {url}")

    def _fetch_once(self, validated: ValidatedUrl) -> TransportResponse:
        last_error: OSError | None = None
        for ip_address in validated.resolved_addresses:
            request = TransportRequest(
                url=validated.normalized_url,
                scheme=validated.scheme,
                hostname=validated.hostname,
                port=validated.port,
                target=validated.target,
                host_header=validated.host_header,
                ip_address=ip_address,
            )
            self._wait_for_domain_slot(validated)
            try:
                response = self._requester.fetch(request)
            except OSError as exc:
                last_error = exc
                continue
            self._ensure_connected_peer(request, response)
            return response
        if last_error is not None:
            raise last_error
        raise OSError(f"no reachable public address for {validated.normalized_url}")

    def _wait_for_domain_slot(self, validated: ValidatedUrl) -> None:
        domain_key = validated.registrable_domain or validated.hostname
        now = self._clock.monotonic()
        last = self._last_request_by_domain.get(domain_key)
        if last is not None:
            elapsed = now - last
            if elapsed < 2.0:
                self._sleeper(2.0 - elapsed)
        self._last_request_by_domain[domain_key] = self._clock.monotonic()

    def _ensure_connected_peer(self, request: TransportRequest, response: TransportResponse) -> None:
        expected = ipaddress.ip_address(request.ip_address)
        actual = ipaddress.ip_address(response.peer_ip)
        if actual != expected:
            raise UnsafeUrlError(
                "connected peer IP did not match validated resolution: "
                f"{actual.compressed} != {expected.compressed}"
            )
        if not actual.is_global:
            raise UnsafeUrlError(f"connected peer is not globally routable: {actual.compressed}")


class ScraplingPageFetcher:
    def __init__(self, transport: SafeHttpTransport | None = None) -> None:
        self._transport = transport or SafeHttpTransport()

    def fetch(self, url: str) -> FetchedPage:
        response = self._transport.get(url)
        return FetchedPage(
            final_url=response.final_url,
            html=_decode_body(response.body, response.headers),
        )


class WebsiteEnrichmentService:
    def __init__(
        self,
        *,
        page_fetcher: PageFetcher | None = None,
        url_policy: UrlPolicy | None = None,
        robots_checker: RobotsChecker | None = None,
        requester: Requester | None = None,
        transport: SafeHttpTransport | None = None,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url_policy = url_policy or UrlPolicy()
        safe_clock = clock or _SystemClock()
        shared_transport = transport
        if shared_transport is None and (page_fetcher is None or robots_checker is None):
            shared_transport = SafeHttpTransport(
                url_policy=self._url_policy,
                requester=requester,
                clock=safe_clock,
                sleeper=sleeper,
            )

        self._page_fetcher = page_fetcher or ScraplingPageFetcher(shared_transport)
        self._robots_checker = robots_checker or _RobotFileRobotsChecker(transport=shared_transport)
        self._clock = safe_clock
        self._sleeper = sleeper

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
            for page_url in _discover_candidate_pages(root_page.final_url, root_page.html, allowed_domain):
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
        if not self._is_allowed_by_robots(validated_url, warnings):
            return None

        page = self._page_fetcher.fetch(validated_url)
        final_url = self._url_policy.validate(
            page.final_url,
            allowed_registrable_domain=allowed_domain,
        )
        if not self._is_allowed_by_robots(final_url, warnings):
            return None
        return FetchedPage(final_url=final_url, html=page.html)

    def _is_allowed_by_robots(self, url: str, warnings: list[str]) -> bool:
        allowed, warning = self._robots_checker.allows(url)
        if warning is not None and warning not in warnings:
            warnings.append(warning)
        return allowed

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


class _SocketRequester:
    def __init__(
        self,
        timeout_seconds: float = 10.0,
        ssl_context_factory: Callable[[], ssl.SSLContext] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._ssl_context_factory = ssl_context_factory or ssl.create_default_context

    def fetch(self, request: TransportRequest) -> TransportResponse:
        raw_socket = socket.create_connection(
            (request.ip_address, request.port),
            timeout=self._timeout_seconds,
        )
        connection: socket.socket | ssl.SSLSocket = raw_socket
        try:
            if request.scheme == "https":
                context = self._ssl_context_factory()
                connection = context.wrap_socket(raw_socket, server_hostname=request.hostname)

            request_bytes = _build_http_request(request)
            connection.sendall(request_bytes)

            response = http.client.HTTPResponse(connection)
            response.begin()
            body = response.read()
            peer_ip = connection.getpeername()[0]
            headers = {key: value for key, value in response.getheaders()}
            return TransportResponse(
                final_url=request.url,
                status_code=response.status,
                headers=headers,
                body=body,
                peer_ip=str(peer_ip),
            )
        finally:
            connection.close()


class _RobotFileRobotsChecker:
    def __init__(
        self,
        transport: SafeHttpTransport | None = None,
        user_agent: str = "*",
    ) -> None:
        self._transport = transport or SafeHttpTransport()
        self._user_agent = user_agent
        self._cache: dict[str, tuple[RobotFileParser | None, str | None]] = {}

    def allows(self, url: str) -> tuple[bool, str | None]:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        parser, warning = self._cache.get(origin, (None, None))
        if origin not in self._cache:
            parser, warning = self._load_parser(origin, url)
            self._cache[origin] = (parser, warning)
        if parser is None:
            return True, warning
        return bool(parser.can_fetch(self._user_agent, url)), warning

    def _load_parser(self, origin: str, url: str) -> tuple[RobotFileParser | None, str | None]:
        robots_url = f"{origin}/robots.txt"
        allowed_domain = registrable_domain(url)
        try:
            response = self._transport.get(
                robots_url,
                allowed_registrable_domain=allowed_domain,
            )
        except (OSError, UnsafeUrlError, ValueError) as exc:
            return None, f"robots unavailable for {robots_url}: {exc}"

        parser = RobotFileParser(robots_url)
        if response.status_code >= 400:
            parser.parse(())
            return parser, None

        body = _decode_body(response.body, response.headers)
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
        if priority is None:
            continue

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


def _link_priority(text: str, path: str) -> int | None:
    combined = f"{text} {path}".casefold()
    if "contact" in combined:
        return 0
    if "about" in combined:
        return 1
    if "team" in combined:
        return 2
    return None


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


def _build_http_request(request: TransportRequest) -> bytes:
    headers = [
        f"GET {request.target} HTTP/1.1",
        f"Host: {request.host_header}",
        "User-Agent: mapslead/0.1",
        "Accept: text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Encoding: identity",
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(headers).encode("ascii")


def _decode_body(body: bytes, headers: Mapping[str, str]) -> str:
    content_type = _header_value(headers, "Content-Type") or ""
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().casefold() == "charset" and value.strip():
            charset = value.strip()
            break
    return body.decode(charset, errors="replace")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    target = name.casefold()
    for key, value in headers.items():
        if key.casefold() == target:
            return value
    return None
