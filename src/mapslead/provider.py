from __future__ import annotations

import csv
import re
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Literal, Protocol, TextIO, TypedDict

from pydantic import ValidationError

from mapslead.errors import ProviderSetupError
from mapslead.models import ProviderCandidate, ProviderRequest, ProviderResult
from mapslead.ports import CandidateSink, MapsProvider


def _canonicalize(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


MAX_CSV_FIELD_SIZE: Final[int] = 1_000_000
DIAGNOSTIC_TAIL_LIMIT: Final[int] = 4_000
RESULTS_PER_DEPTH: Final[int] = 20
BLOCKING_SIGNALS: Final[tuple[str, ...]] = (
    "captcha",
    "unusual traffic",
    "too many requests",
    "rate limit",
)
FIELD_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "place_id": ("place_id", "placeId", "placeid"),
    "name": ("name", "title", "business_name"),
    "category": ("category", "categories", "types"),
    "address": ("address", "full_address"),
    "phone": ("phone", "phone_number", "telephone"),
    "website": ("website", "site"),
    "rating": ("rating", "totalScore", "score"),
    "review_count": ("review_count", "reviews", "reviewsCount"),
    "google_maps_url": ("google_maps_url", "url", "link"),
    "status": ("status", "result_status"),
}
CANONICAL_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    field_name: tuple(_canonicalize(alias) for alias in aliases)
    for field_name, aliases in FIELD_ALIASES.items()
}


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    returncode: int
    stdout_tail: str
    stderr_tail: str
    interrupted: bool


class ProcessRunner(Protocol):
    def run(self, args: list[str], cwd: Path) -> ProcessOutcome: ...


class _TailBuffer:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._value = ""

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        combined = f"{self._value}{chunk}"
        if len(combined) <= self._limit:
            self._value = combined
            return
        self._value = combined[-self._limit :]

    @property
    def value(self) -> str:
        return self._value


class SubprocessRunner:
    def run(self, args: list[str], cwd: Path) -> ProcessOutcome:
        try:
            process = subprocess.Popen(
                args,
                cwd=cwd,
                shell=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise ProviderSetupError(f"Unable to start Docker provider: {error}") from error

        stdout_buffer = _TailBuffer(DIAGNOSTIC_TAIL_LIMIT)
        stderr_buffer = _TailBuffer(DIAGNOSTIC_TAIL_LIMIT)
        readers = _start_reader_threads(process, stdout_buffer, stderr_buffer)
        interrupted = False
        try:
            process.wait()
            returncode = process.returncode if process.returncode is not None else 1
        except KeyboardInterrupt:
            interrupted = True
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    process.kill()
                process.wait()
            returncode = 130
        finally:
            for reader in readers:
                reader.join()
            _close_stream(process.stdout)
            _close_stream(process.stderr)

        return ProcessOutcome(
            returncode=returncode,
            stdout_tail=stdout_buffer.value,
            stderr_tail=stderr_buffer.value,
            interrupted=interrupted,
        )


@dataclass(frozen=True, slots=True)
class _IngestResult:
    candidate_count: int
    rejected_row_count: int
    blocked: bool
    parse_error: bool
    row_diagnostics: tuple[str, ...]


class _CandidatePayload(TypedDict, total=False):
    place_id: str
    name: str
    category: str
    address: str
    phone: str
    website: str
    rating: float
    review_count: int
    google_maps_url: str


class GosomDockerProvider(MapsProvider):
    def __init__(self, process_runner: ProcessRunner | None = None) -> None:
        self._process_runner = process_runner or SubprocessRunner()

    def replay(self, request: ProviderRequest, sink: CandidateSink) -> ProviderResult:
        candidate_count = 0
        rejected_row_count = 0
        blocked = False
        parse_error = False
        row_diagnostics: list[str] = []

        for results_path in _attempt_results_paths(request.provider_dir):
            ingest_result = _ingest_results(results_path, sink)
            candidate_count += ingest_result.candidate_count
            rejected_row_count += ingest_result.rejected_row_count
            blocked = blocked or ingest_result.blocked
            parse_error = parse_error or ingest_result.parse_error
            row_diagnostics.extend(ingest_result.row_diagnostics)

        return ProviderResult(
            status="blocked" if blocked else "failed" if parse_error else "completed",
            candidate_count=candidate_count,
            rejected_row_count=rejected_row_count,
            diagnostics_tail=_compose_diagnostics("", "", row_diagnostics),
        )

    def acquire(self, request: ProviderRequest, sink: CandidateSink) -> ProviderResult:
        attempt_dir = _next_attempt_dir(request.provider_dir)
        out_dir = attempt_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=False)
        queries_path = attempt_dir / "queries.txt"
        queries_path.write_text(
            f"{request.search_query} in {request.location}\n",
            encoding="utf-8",
        )

        outcome = self._process_runner.run(
            _docker_args(
                queries_path,
                out_dir,
                max_new_records=request.max_new_records,
                language=request.language,
            ),
            attempt_dir,
        )
        ingest_result = _ingest_results(out_dir / "results.csv", sink)
        diagnostics_tail = _compose_diagnostics(
            outcome.stdout_tail,
            outcome.stderr_tail,
            list(ingest_result.row_diagnostics),
        )

        status: Literal["completed", "partial", "blocked", "failed"] = "completed"
        if outcome.interrupted:
            status = "partial"
        elif ingest_result.blocked or _diagnostics_indicate_blocked(diagnostics_tail):
            status = "blocked"
        elif ingest_result.parse_error or outcome.returncode != 0:
            status = "failed"

        return ProviderResult(
            status=status,
            candidate_count=ingest_result.candidate_count,
            rejected_row_count=ingest_result.rejected_row_count,
            diagnostics_tail=diagnostics_tail,
            interrupted=outcome.interrupted,
        )


def _tail_text(value: str) -> str:
    if len(value) <= DIAGNOSTIC_TAIL_LIMIT:
        return value
    return value[-DIAGNOSTIC_TAIL_LIMIT:]


def _start_reader_threads(
    process: subprocess.Popen[str],
    stdout_buffer: _TailBuffer,
    stderr_buffer: _TailBuffer,
) -> tuple[threading.Thread, ...]:
    readers: list[threading.Thread] = []
    if process.stdout is not None:
        stdout_reader = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_buffer),
            daemon=True,
        )
        stdout_reader.start()
        readers.append(stdout_reader)
    if process.stderr is not None:
        stderr_reader = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_buffer),
            daemon=True,
        )
        stderr_reader.start()
        readers.append(stderr_reader)
    return tuple(readers)


def _drain_stream(stream: TextIO, buffer: _TailBuffer) -> None:
    with suppress(OSError, ValueError):
        while True:
            chunk = stream.read(4096)
            if chunk == "":
                break
            buffer.append(chunk)


def _close_stream(stream: IO[str] | None) -> None:
    if stream is None:
        return
    with suppress(OSError, ValueError):
        stream.close()


def _docker_depth(max_new_records: int) -> int:
    return max(1, (max_new_records + RESULTS_PER_DEPTH - 1) // RESULTS_PER_DEPTH)


def _docker_args(
    queries_path: Path,
    out_dir: Path,
    *,
    max_new_records: int,
    language: str,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{queries_path.resolve()}:/queries.txt:ro",
        "-v",
        f"{out_dir.resolve()}:/out",
        "gosom/google-maps-scraper",
        "-input",
        "/queries.txt",
        "-results",
        "/out/results.csv",
        "-lang",
        language,
        "-depth",
        str(_docker_depth(max_new_records)),
        "-c",
        "1",
        "-exit-on-inactivity",
        "3m",
    ]


def _next_attempt_dir(provider_dir: Path) -> Path:
    provider_dir.mkdir(parents=True, exist_ok=True)
    next_attempt = 1
    for child in provider_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith("attempt-"):
            continue
        suffix = name.removeprefix("attempt-")
        if suffix.isdigit():
            next_attempt = max(next_attempt, int(suffix) + 1)
    attempt_dir = provider_dir / f"attempt-{next_attempt:04d}"
    attempt_dir.mkdir(parents=False, exist_ok=False)
    return attempt_dir


def _attempt_results_paths(provider_dir: Path) -> tuple[Path, ...]:
    if not provider_dir.exists():
        return ()

    attempts: list[tuple[int, Path]] = []
    for child in provider_dir.iterdir():
        if not child.is_dir() or not child.name.startswith("attempt-"):
            continue
        suffix = child.name.removeprefix("attempt-")
        if not suffix.isdigit():
            continue
        results_path = child / "out" / "results.csv"
        if results_path.is_file():
            attempts.append((int(suffix), results_path))
    attempts.sort(key=lambda item: item[0])
    return tuple(results_path for _, results_path in attempts)


def _ingest_results(results_path: Path, sink: CandidateSink) -> _IngestResult:
    if not results_path.is_file():
        return _IngestResult(
            candidate_count=0,
            rejected_row_count=0,
            blocked=False,
            parse_error=False,
            row_diagnostics=(),
        )

    previous_field_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_CSV_FIELD_SIZE)
    candidate_count = 0
    rejected_row_count = 0
    blocked = False
    parse_error = False
    row_diagnostics: list[str] = []

    try:
        with results_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            try:
                fieldnames = reader.fieldnames
            except csv.Error as error:
                parse_error = True
                rejected_row_count += 1
                row_diagnostics.append(_csv_error_diagnostic(reader.line_num or 1, error))
                fieldnames = ()
            if fieldnames is None:
                return _IngestResult(
                    candidate_count=0,
                    rejected_row_count=0,
                    blocked=False,
                    parse_error=False,
                    row_diagnostics=(),
                )
            if parse_error:
                return _IngestResult(
                    candidate_count=candidate_count,
                    rejected_row_count=rejected_row_count,
                    blocked=blocked,
                    parse_error=True,
                    row_diagnostics=tuple(row_diagnostics),
                )

            for line_number, row in enumerate(reader, start=2):
                normalized_row = {
                    _canonicalize(key): value for key, value in row.items() if key is not None
                }
                if _row_is_empty(normalized_row):
                    continue

                blocked_status = _first_value(normalized_row, CANONICAL_ALIASES["status"])
                if blocked_status is not None and blocked_status.strip().lower() == "blocked":
                    blocked = True
                    row_diagnostics.append(f"row {line_number}: provider status blocked")
                    continue

                try:
                    candidate_data = _candidate_data_from_row(normalized_row)
                    candidate = ProviderCandidate(**candidate_data)
                except (ValidationError, ValueError) as error:
                    rejected_row_count += 1
                    if isinstance(error, ValidationError):
                        detail = error.errors()[0]["msg"]
                    else:
                        detail = str(error)
                    row_diagnostics.append(f"row {line_number}: {detail}")
                    continue

                sink(candidate)
                candidate_count += 1
    except csv.Error as error:
        parse_error = True
        rejected_row_count += 1
        row_diagnostics.append(_csv_error_diagnostic(candidate_count + rejected_row_count + 1, error))
    finally:
        csv.field_size_limit(previous_field_limit)

    return _IngestResult(
        candidate_count=candidate_count,
        rejected_row_count=rejected_row_count,
        blocked=blocked,
        parse_error=parse_error,
        row_diagnostics=tuple(row_diagnostics),
    )


def _row_is_empty(row: dict[str, str | None]) -> bool:
    return not any((value or "").strip() for value in row.values())


def _candidate_data_from_row(row: dict[str, str | None]) -> _CandidatePayload:
    candidate_data: _CandidatePayload = {}

    place_id = _first_value(row, CANONICAL_ALIASES["place_id"])
    if place_id is not None:
        candidate_data["place_id"] = place_id

    name = _first_value(row, CANONICAL_ALIASES["name"])
    if name is not None:
        candidate_data["name"] = name

    category = _first_value(row, CANONICAL_ALIASES["category"])
    if category is not None:
        candidate_data["category"] = category

    address = _first_value(row, CANONICAL_ALIASES["address"])
    if address is not None:
        candidate_data["address"] = address

    phone = _first_value(row, CANONICAL_ALIASES["phone"])
    if phone is not None:
        candidate_data["phone"] = phone

    website = _first_value(row, CANONICAL_ALIASES["website"])
    if website is not None:
        candidate_data["website"] = website

    rating = _first_value(row, CANONICAL_ALIASES["rating"])
    if rating is not None:
        candidate_data["rating"] = float(rating)

    review_count = _first_value(row, CANONICAL_ALIASES["review_count"])
    if review_count is not None:
        candidate_data["review_count"] = int(review_count)

    google_maps_url = _first_value(row, CANONICAL_ALIASES["google_maps_url"])
    if google_maps_url is not None:
        candidate_data["google_maps_url"] = google_maps_url

    return candidate_data


def _first_value(row: dict[str, str | None], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        raw_value = row.get(alias)
        if raw_value is None:
            continue
        value = raw_value.strip()
        if value != "":
            return value
    return None


def _compose_diagnostics(
    stdout_tail: str,
    stderr_tail: str,
    row_diagnostics: list[str],
) -> str:
    diagnostics: list[str] = []
    if stdout_tail:
        diagnostics.append(stdout_tail)
    if stderr_tail:
        diagnostics.append(stderr_tail)
    diagnostics.extend(row_diagnostics)
    return _tail_text("\n".join(diagnostics))


def _diagnostics_indicate_blocked(diagnostics_tail: str) -> bool:
    normalized = diagnostics_tail.lower()
    if any(signal in normalized for signal in BLOCKING_SIGNALS):
        return True
    return (
        re.search(
            r"(?m)(?:^\s*429\s*$|\bhttp(?:/\d(?:\.\d)?)?\s*429\b|"
            r"\b(?:response\s+)?status(?:\s+code)?\s*[:=]?\s*429\b)",
            normalized,
        )
        is not None
    )


def _csv_error_diagnostic(line_number: int, error: csv.Error) -> str:
    return f"row {line_number}: csv parse error: {error}"
