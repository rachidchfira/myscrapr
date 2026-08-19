from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")
_LANGUAGE_CODE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def validate_search_query(query: str) -> str:
    return _validate_human_text(query, field_name="search query")


def validate_location_query(location: str) -> str:
    return _validate_human_text(location, field_name="location")


def validate_language_code(language: str) -> str:
    normalized = _validate_human_text(language, field_name="language")
    if " " in normalized or _LANGUAGE_CODE_RE.fullmatch(normalized) is None:
        raise ValueError("language must be a simple code like en or vi")
    return normalized.casefold()


def _validate_human_text(value: str, *, field_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(f"{field_name} cannot contain control characters")
    collapsed = _WHITESPACE_RE.sub(" ", normalized).strip()
    if not collapsed:
        raise ValueError(f"{field_name} is required")
    return collapsed
