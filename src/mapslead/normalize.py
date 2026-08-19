from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from tld import get_tld
from tld.exceptions import TldBadUrl, TldDomainNotFound

from mapslead.models import Identity, ProviderCandidate

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip()
    return _WHITESPACE_RE.sub(" ", normalized).casefold()


def normalize_phone(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip()
    has_plus = normalized.startswith("+")
    digits = "".join(character for character in normalized if character.isdigit())
    if has_plus and digits:
        return f"+{digits}"
    return digits


def registrable_domain(value: str | None) -> str | None:
    hostname = _normalized_hostname(value)
    if hostname is None:
        return None
    try:
        result = get_tld(f"https://{hostname}", as_object=True, fix_protocol=True)
    except (TldBadUrl, TldDomainNotFound, ValueError):
        return None
    if result is None or isinstance(result, str):
        return None
    return result.fld.casefold()


def build_identity(candidate: ProviderCandidate) -> Identity:
    normalized_name = normalize_text(candidate.name)
    if not normalized_name:
        raise ValueError("candidate name is required for identity")

    keys: list[str] = []
    place_id = _trimmed(candidate.place_id)
    if place_id is not None:
        keys.append(f"place:{place_id}")

    normalized_address = normalize_text(candidate.address)
    normalized_phone = normalize_phone(candidate.phone)
    normalized_domain = registrable_domain(candidate.website)

    if normalized_address:
        keys.append(f"name_address:{normalized_name}|{normalized_address}")
    if normalized_phone:
        keys.append(f"name_phone:{normalized_name}|{normalized_phone}")
    if normalized_domain:
        keys.append(f"name_domain:{normalized_name}|{normalized_domain}")

    if not keys:
        raise ValueError("candidate identity is incomplete")

    primary_key = keys[0]
    aliases = tuple(dict.fromkeys(key for key in keys[1:] if key != primary_key))
    return Identity(primary_key=primary_key, aliases=aliases)


def _normalized_hostname(value: str | None) -> str | None:
    trimmed = _trimmed(value)
    if trimmed is None:
        return None
    parsed = urlsplit(trimmed)
    hostname = parsed.hostname
    if hostname is None:
        parsed = urlsplit(f"https://{trimmed}")
        hostname = parsed.hostname
    if hostname is not None:
        hostname = hostname.casefold()
    if hostname is None:
        return None
    return hostname.rstrip(".")


def _trimmed(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
