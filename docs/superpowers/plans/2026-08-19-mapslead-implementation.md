# MapsLead Hybrid CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Python CLI that acquires Google Maps businesses through the `gosom/google-maps-scraper` Docker image, enriches their public websites with Scrapling, enforces an atomic 1,000-new-record daily quota, and exports deterministic CSV and JSON.

**Architecture:** A Python orchestration layer isolates the Docker provider, website fetching, SQLite persistence, and exports behind typed interfaces. SQLite is the authority for identity, immutable run snapshots, quota, and resume state; external I/O is injected so the default test suite is deterministic and offline.

**Tech Stack:** Python 3.12, Typer, Pydantic 2, Scrapling 0.4.x, SQLite, pytest, Ruff, mypy

**Spec:** `docs/superpowers/specs/2026-08-19-mapslead-design.md`

## Global Constraints

- The public workflow is manually launched from a local CLI.
- The only Google Maps provider in version 1 is `gosom/google-maps-scraper` through Docker.
- The hard quota is 1,000 newly accepted unique records per `Asia/Ho_Chi_Minh` calendar day and cannot be overridden.
- Only new unique records consume quota; duplicates, resume, exports, rejected candidates, and enrichment retries do not.
- Google Place ID is the preferred identity; fallback tiers are name+address, name+phone, then name+registrable website domain.
- Every accepted candidate requires a non-empty normalized name and either Place ID or a complete fallback pair.
- Website enrichment stays on the original registrable domain, visits the homepage plus at most three Contact/About/Team pages, and obeys applicable robots rules.
- Local, loopback, link-local, private-network, non-HTTP, authentication, and download destinations are rejected.
- CAPTCHA solving, proxy rotation, account automation, and anti-bot bypass are out of scope.
- Generated databases, raw output, logs, temporary files, and exports are Git-ignored.
- Tests run offline by default; live Docker/Maps testing is opt-in only.

## File Structure

```text
mapslead/
├── .gitignore                         generated and environment files
├── README.md                          installation and operator workflow
├── pyproject.toml                     package, CLI, dependencies, tool config
├── src/mapslead/__init__.py           package version
├── src/mapslead/cli.py                Typer commands and exit messages
├── src/mapslead/config.py             paths, timezone, fixed limits
├── src/mapslead/models.py             validated domain models and enums
├── src/mapslead/ports.py              cross-module protocols
├── src/mapslead/errors.py             typed application errors
├── src/mapslead/normalize.py          identity and contact normalization
├── src/mapslead/repository.py         SQLite schema, quota, snapshots, resume
├── src/mapslead/provider.py           Docker provider and CSV translation
├── src/mapslead/enrichment.py         URL safety, robots, page selection, extraction
├── src/mapslead/exporter.py           deterministic atomic CSV/JSON export
├── src/mapslead/service.py            scrape/resume orchestration
├── tests/fixtures/provider/results.csv
├── tests/fixtures/web/home.html
├── tests/fixtures/web/contact.html
├── tests/test_models.py
├── tests/test_repository.py
├── tests/test_provider.py
├── tests/test_enrichment.py
├── tests/test_exporter.py
├── tests/test_service.py
└── tests/test_cli.py
```

---

### Task 1: Package scaffold, models, configuration, and normalization

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/mapslead/__init__.py`
- Create: `src/mapslead/config.py`
- Create: `src/mapslead/models.py`
- Create: `src/mapslead/ports.py`
- Create: `src/mapslead/errors.py`
- Create: `src/mapslead/normalize.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces: `Settings`, `RunStatus`, `EnrichmentStatus`, `ProviderCandidate`, `Identity`, `ProviderRequest`, `ProviderResult`, `EnrichmentResult`, `Acceptance`, `RunRecord`, `RunSnapshot`, `ExportPaths`, `ProgressEvent`
- Produces: frozen `RepositoryPort`, `MapsProvider`, `CandidateSink`, `PageFetcher`, `WebsiteEnricher`, `ExporterPort`, `ProgressSink`, and `Clock` protocols used by later tasks
- Produces: `MapsLeadError`, `QuotaExceededError`, `UnsafeUrlError`, `ProviderSetupError`, `RunStateError`, and `ExportError`
- Produces: `normalize_text`, `normalize_phone`, `registrable_domain`, `build_identity`
- Consumes: no application interfaces

- [ ] **Step 1: Add the failing domain-model tests**

Create `tests/test_models.py` with literal expectations that prove the identity priority and rejection boundary:

```python
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


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (ProviderCandidate(name="Example Dental", address=" 1 Main  Street "), "name_address:example dental|1 main street"),
        (ProviderCandidate(name="Example Dental", phone="+84 (28) 123-456"), "name_phone:example dental|+8428123456"),
        (ProviderCandidate(name="Example Dental", website="https://www.example.com/contact"), "name_domain:example dental|example.com"),
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
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/test_models.py -q`

Expected: collection fails because the `mapslead` package does not exist.

- [ ] **Step 3: Add the package definition and minimal models**

Create `pyproject.toml` with Python `>=3.12`, the `mapslead = "mapslead.cli:app"` script, runtime dependencies `typer>=0.16,<1`, `pydantic>=2.11,<3`, `platformdirs>=4.3,<5`, `scrapling[fetchers]>=0.4.14,<0.5`, and dev dependencies `pytest>=8.4,<9`, `pytest-cov>=6,<7`, `ruff>=0.12,<1`, `mypy>=1.17,<2`, and `build>=1.2,<2`. Configure pytest with `pythonpath = ["src"]`, Ruff for Python 3.12 with an 100-character line length, and mypy with `strict = true`.

Implement:

```python
# src/mapslead/config.py
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

DAILY_NEW_RECORD_LIMIT = 1_000
DEFAULT_RUN_LIMIT = 200

@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    export_dir: Path = Path("exports")
    timezone: ZoneInfo = ZoneInfo("Asia/Ho_Chi_Minh")
```

Add `Settings.from_env()` with exact optional environment overrides `MAPSLEAD_DATA_DIR` and `MAPSLEAD_EXPORT_DIR`; the daily limit and timezone are not environment-configurable. CLI path flags later override these two environment values.

Use `pydantic.BaseModel` for provider input and output models. `ProviderCandidate` contains all Maps fields from the spec with optional values and no implicit identity validation; `build_identity` owns cross-field identity validation. `Identity` contains `primary_key: str` and `aliases: tuple[str, ...]`.

Define `ProviderRequest` with exact fields `business: str`, `location: str`, `provider_dir: Path`, and `max_new_records: int`. Define `ProviderResult` with `status: Literal["completed", "partial", "blocked", "failed"]`, `candidate_count: int`, `rejected_row_count: int`, `diagnostics_tail: str`, and `interrupted: bool = False`. The frozen `MapsProvider` protocol exposes both `replay(request, sink) -> ProviderResult` for durable prior attempts and `acquire(request, sink) -> ProviderResult` for one new attempt.

Put cross-module protocols in `ports.py`; later tasks implement them without changing their method names or parameter types. Put user-facing typed failures in `errors.py`; expected CLI failures must derive from `MapsLeadError` rather than leaking dependency exceptions.

Implement normalization with Unicode NFKC, whitespace collapse, `casefold()`, phone digit retention with one leading `+`, URL hostname normalization, and Scrapling's installed `tld` dependency for registrable domains.

- [ ] **Step 4: Verify GREEN and quality checks**

Run: `python -m pytest tests/test_models.py -q`

Expected: all model tests pass.

Run: `python -m ruff check src/mapslead tests/test_models.py && python -m mypy src/mapslead`

Expected: exit code 0.

- [ ] **Step 5: Commit Task 1**

```bash
git add .gitignore pyproject.toml src/mapslead tests/test_models.py
git commit -m "feat: scaffold MapsLead domain models"
```

---

### Task 2: SQLite repository, immutable snapshots, quota, and deduplication

**Files:**
- Create: `src/mapslead/repository.py`
- Create: `tests/test_repository.py`

**Interfaces:**
- Consumes: `Settings`, `ProviderCandidate`, `Identity`, `Acceptance`, `RunRecord`, `RunStatus`, `EnrichmentResult`
- Produces: `Repository.initialize`, `create_run`, `get_run`, `remaining_quota`, `new_unique_count_for_run`, `accept_candidate`, `pending_enrichment`, `save_enrichment`, `set_run_status`, `snapshots_for_run`

- [ ] **Step 1: Write failing repository behavior tests**

Create `tests/test_repository.py` using real temporary SQLite databases. Cover:

```python
def test_only_new_business_consumes_daily_quota(repository, candidate, now):
    run_a = repository.create_run("dentists", "HCMC", 10, now)
    first = repository.accept_candidate(run_a.id, candidate, now)
    second = repository.accept_candidate(run_a.id, candidate, now)
    assert first.is_new is True
    assert second.is_new is False
    assert repository.remaining_quota(now) == 999
    assert len(repository.snapshots_for_run(run_a.id)) == 1


def test_fallback_alias_matches_later_candidate_with_place_id(repository, now):
    run = repository.create_run("dentists", "HCMC", 10, now)
    first = ProviderCandidate(name="Example", address="1 Main St", phone="+84123")
    later = ProviderCandidate(name="Example", address="1 Main St", place_id="ChIJ-new")
    assert repository.accept_candidate(run.id, first, now).is_new is True
    assert repository.accept_candidate(run.id, later, now).is_new is False


def test_old_run_snapshot_is_immutable_after_later_sighting(repository, now):
    first_run = repository.create_run("dentists", "HCMC", 10, now)
    original = ProviderCandidate(name="Example", place_id="ChIJ-1", phone="111")
    repository.accept_candidate(first_run.id, original, now)
    second_run = repository.create_run("dentists", "HCMC", 10, now)
    changed = ProviderCandidate(name="Example", place_id="ChIJ-1", phone="222")
    repository.accept_candidate(second_run.id, changed, now)
    assert repository.snapshots_for_run(first_run.id)[0].phone == "111"
    assert repository.snapshots_for_run(second_run.id)[0].phone == "222"
```

Also test the Asia/Ho_Chi_Minh midnight boundary, rejected candidates not consuming quota, unique `(run_id, business_id)` associations, reuse eligibility for enrichment, and two repository connections racing for the final quota slot with exactly one acceptance succeeding.

Test that `new_unique_count_for_run(run_id)` increases only when `accept_candidate` returns `is_new=True`, remains unchanged for duplicates/replay, and survives repository close/reopen.

- [ ] **Step 2: Run repository tests and verify RED**

Run: `python -m pytest tests/test_repository.py -q`

Expected: import fails for `mapslead.repository`.

- [ ] **Step 3: Implement schema and transactional repository**

Create schema version 1 with tables:

```sql
schema_version(version INTEGER NOT NULL);
businesses(id INTEGER PRIMARY KEY, place_id TEXT UNIQUE, canonical_json TEXT NOT NULL,
           first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
identity_aliases(alias TEXT PRIMARY KEY, business_id INTEGER NOT NULL REFERENCES businesses(id));
runs(id TEXT PRIMARY KEY, business_type TEXT NOT NULL, location_query TEXT NOT NULL,
     requested_limit INTEGER NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
     finished_at TEXT, provider_dir TEXT NOT NULL, error TEXT,
     new_unique_count INTEGER NOT NULL DEFAULT 0 CHECK(new_unique_count >= 0));
run_businesses(run_id TEXT NOT NULL REFERENCES runs(id),
               business_id INTEGER NOT NULL REFERENCES businesses(id),
               snapshot_json TEXT NOT NULL, enrichment_status TEXT NOT NULL,
               enrichment_error TEXT, PRIMARY KEY(run_id, business_id));
daily_quota(day TEXT PRIMARY KEY, accepted_count INTEGER NOT NULL CHECK(accepted_count BETWEEN 0 AND 1000));
```

Use WAL mode, `PRAGMA foreign_keys=ON`, a busy timeout, and `BEGIN IMMEDIATE` inside `accept_candidate`. Re-check quota after the write lock is held. When and only when a business is newly inserted, increment the same transaction's daily quota counter and the owning run's `new_unique_count`. Compute the quota day by converting the supplied aware datetime to `Asia/Ho_Chi_Minh`. Store Pydantic JSON snapshots. Add aliases only after resolving collisions to the already matched business; never reassign an alias from one business to another.

- [ ] **Step 4: Verify repository GREEN**

Run: `python -m pytest tests/test_repository.py -q`

Expected: all repository tests pass, including the race test.

Run: `python -m ruff check src/mapslead/repository.py tests/test_repository.py && python -m mypy src/mapslead`

Expected: exit code 0.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/mapslead/repository.py tests/test_repository.py
git commit -m "feat: add durable quota and business repository"
```

---

### Task 3: Docker Maps provider and durable CSV ingestion

**Files:**
- Create: `src/mapslead/provider.py`
- Create: `tests/fixtures/provider/results.csv`
- Create: `tests/test_provider.py`

**Interfaces:**
- Consumes: `ProviderRequest`, `ProviderCandidate`, `ProviderResult`
- Consumes: frozen `CandidateSink` and `MapsProvider` protocols from `ports.py`
- Produces: private `ProcessRunner` protocol, `SubprocessRunner`, `GosomDockerProvider.acquire`

- [ ] **Step 1: Write failing provider tests**

Build a fake `ProcessRunner` that records its argument list and writes controlled CSV output. Test that:

- The command is an argument list beginning `docker run --rm`, never a shell string.
- The query file contains exactly `<business> in <location>\n`.
- The provider uses image `gosom/google-maps-scraper`, `-depth 1`, `-c 1`, and `-exit-on-inactivity 3m`.
- Common provider column aliases map to the validated internal model.
- Malformed rows are rejected without reaching the sink.
- Each complete row reaches the sink once.
- Diagnostics containing `captcha`, `unusual traffic`, `too many requests`, `rate limit`, `429`, or a provider blocked status produce `ProviderResult(status="blocked")`.
- A non-zero exit without a blocking signal produces `failed`; exit zero with no rows produces `completed`.
- A `KeyboardInterrupt` terminates the process, parses complete CSV rows already on disk, and returns `ProviderResult(status="partial", interrupted=True)`; it does not re-raise or invent a second interruption channel.
- `replay()` parses every durable attempt CSV in numeric attempt order without starting Docker, while `acquire()` creates exactly one new numeric attempt directory and starts Docker once.

- [ ] **Step 2: Run provider tests and verify RED**

Run: `python -m pytest tests/test_provider.py -q`

Expected: import fails for `mapslead.provider`.

- [ ] **Step 3: Implement the provider boundary**

Define:

```python
class CandidateSink(Protocol):
    def __call__(self, candidate: ProviderCandidate) -> None: ...

class ProcessRunner(Protocol):
    def run(self, args: list[str], cwd: Path) -> ProcessOutcome: ...

class GosomDockerProvider:
    def replay(self, request: ProviderRequest, sink: CandidateSink) -> ProviderResult: ...
    def acquire(self, request: ProviderRequest, sink: CandidateSink) -> ProviderResult: ...
```

`ProcessOutcome` has `returncode: int`, `stdout_tail: str`, `stderr_tail: str`, and `interrupted: bool`. `SubprocessRunner` catches `KeyboardInterrupt`, sends terminate, waits five seconds, kills only that child if necessary, and returns code 130 with `interrupted=True`.

For each acquisition create `<provider_dir>/attempt-<NNNN>/queries.txt` and `<provider_dir>/attempt-<NNNN>/out/results.csv`, choosing one greater than the largest existing attempt number. Construct mounts with resolved absolute paths:

```text
docker run --rm
-v <queries-file>:/queries.txt:ro
-v <out-dir>:/out
gosom/google-maps-scraper
-input /queries.txt
-results /out/results.csv
-depth 1
-c 1
-exit-on-inactivity 3m
```

Use `subprocess.Popen(args, shell=False, text=True)` in `SubprocessRunner`. Capture bounded stdout/stderr tails for diagnostics. Parse CSV with `csv.DictReader`, a maximum field size, explicit aliases, Pydantic validation, and per-row errors collected into `ProviderResult`.

`replay()` discovers `attempt-*/out/results.csv`, sorts by numeric suffix, and sends every complete validated row to the sink. `acquire()` runs one fresh attempt and then ingests that attempt's CSV. Replayed rows can repeat; repository aliases and the unique run association make replay idempotent.

- [ ] **Step 4: Verify provider GREEN**

Run: `python -m pytest tests/test_provider.py -q`

Expected: all provider tests pass.

Run: `python -m ruff check src/mapslead/provider.py tests/test_provider.py && python -m mypy src/mapslead`

Expected: exit code 0.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/mapslead/provider.py tests/fixtures/provider tests/test_provider.py
git commit -m "feat: integrate local Google Maps Docker provider"
```

---

### Task 4: Safe same-domain website enrichment

**Files:**
- Create: `src/mapslead/enrichment.py`
- Create: `tests/fixtures/web/home.html`
- Create: `tests/fixtures/web/contact.html`
- Create: `tests/test_enrichment.py`

**Interfaces:**
- Consumes: `ProviderCandidate`, `EnrichmentResult`, `registrable_domain`
- Consumes: frozen `PageFetcher` and `WebsiteEnricher` protocols from `ports.py`
- Produces: private `Resolver` and `RobotsChecker` protocols, `FetchedPage`, `UrlPolicy`, `ScraplingPageFetcher`, `WebsiteEnrichmentService.enrich`

- [ ] **Step 1: Write failing enrichment tests**

Use real HTML fixtures and a deterministic fake fetcher. Cover:

```python
def test_enrichment_prefers_contact_then_about_then_team_and_caps_four_pages():
    result = enricher.enrich("https://example.com")
    assert fetcher.requested_urls == [
        "https://example.com",
        "https://example.com/contact",
        "https://example.com/about",
        "https://example.com/team",
    ]
    assert result.emails == ("hello@example.com", "sales@example.com")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/private",
        "https://user:password@example.com",
    ],
)
def test_url_policy_rejects_unsafe_destinations(url, policy):
    with pytest.raises(UnsafeUrlError):
        policy.validate(url)
```

Also cover IPv6 private/link-local targets, DNS resolving to mixed public/private addresses, revalidation after redirects, rejection of cross-registrable-domain redirects, robots disallow, download/authentication link filtering, mailto and visible-email extraction, social normalization for Facebook/Instagram/LinkedIn/X/YouTube, deduplication, per-domain two-second spacing through an injected monotonic clock/sleeper, and a single-page fetch failure becoming an `EnrichmentResult` error instead of an exception escaping the business boundary.

- [ ] **Step 2: Run enrichment tests and verify RED**

Run: `python -m pytest tests/test_enrichment.py -q`

Expected: import fails for `mapslead.enrichment`.

- [ ] **Step 3: Implement safe fetching and extraction**

Use `urllib.parse`, `socket.getaddrinfo`, and `ipaddress.ip_address` in `UrlPolicy`. Require HTTPS or HTTP, no URL credentials, a hostname, and every resolved address to be globally routable. Validate the original URL and each redirect target.

`ScraplingPageFetcher` uses `scrapling.fetchers.Fetcher.get(url)` and returns `FetchedPage(final_url=str(page.url), html=str(page.html_content))`. Page discovery parses anchors from Scrapling's selector API, rejects non-page extensions and `/login`, `/signin`, `/account`, scores Contact before About before Team, deduplicates normalized URLs, and selects three.

Use `urllib.robotparser.RobotFileParser` through the injected `RobotsChecker`; fetch and cache one robots file per origin. If robots retrieval fails, allow the page but retain a warning in the result. Enforce one active request per business and at least two seconds between requests to the same registrable domain.

- [ ] **Step 4: Verify enrichment GREEN**

Run: `python -m pytest tests/test_enrichment.py -q`

Expected: all enrichment tests pass without live network access.

Run: `python -m ruff check src/mapslead/enrichment.py tests/test_enrichment.py && python -m mypy src/mapslead`

Expected: exit code 0.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/mapslead/enrichment.py tests/fixtures/web tests/test_enrichment.py
git commit -m "feat: enrich business websites safely"
```

---

### Task 5: Deterministic atomic CSV and JSON exports

**Files:**
- Create: `src/mapslead/exporter.py`
- Create: `tests/test_exporter.py`

**Interfaces:**
- Consumes: repository run snapshots and `Settings.export_dir`
- Consumes: frozen `ExporterPort` protocol and `ExportPaths` model
- Produces: `Exporter.export_run`

- [ ] **Step 1: Write failing exporter tests**

Test with literal expected rows that:

- Sorting is normalized name, normalized address, then business ID.
- CSV columns exactly match the spec order.
- CSV emails are sorted and semicolon-separated.
- JSON emails are sorted arrays and missing scalar values are `null`.
- Both formats contain one row per run-business association.
- A serialization or rename failure leaves an existing valid export unchanged.
- Successful export atomically replaces both destination files and removes temporary files.

- [ ] **Step 2: Run exporter tests and verify RED**

Run: `python -m pytest tests/test_exporter.py -q`

Expected: import fails for `mapslead.exporter`.

- [ ] **Step 3: Implement atomic deterministic export**

Implement `Exporter.export_run(run_id: str) -> ExportPaths`. Write `results.csv.tmp` and `results.json.tmp` in `exports/<run-id>/`, flush and `os.fsync()` both, then replace the destinations with `os.replace`. If either serialization fails, delete only this attempt's temporary files and leave current destinations untouched. Serialize timestamps as ISO 8601 strings.

- [ ] **Step 4: Verify exporter GREEN**

Run: `python -m pytest tests/test_exporter.py -q`

Expected: all exporter tests pass.

Run: `python -m ruff check src/mapslead/exporter.py tests/test_exporter.py && python -m mypy src/mapslead`

Expected: exit code 0.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/mapslead/exporter.py tests/test_exporter.py
git commit -m "feat: export stable CSV and JSON snapshots"
```

---

### Task 6: Scrape and resume orchestration

**Files:**
- Create: `src/mapslead/service.py`
- Create: `tests/test_service.py`

**Interfaces:**
- Consumes: `Repository`, `GosomDockerProvider`, `WebsiteEnricher`, `Exporter`, domain models
- Consumes: frozen `ProgressSink` protocol
- Produces: `MapsLeadService.scrape`, `MapsLeadService.resume`, `RunOutcome`, typed service errors

- [ ] **Step 1: Write failing service tests**

Use a fake provider that emits real `ProviderCandidate` objects, a fake enricher, the real temporary SQLite repository, and the real exporter. Cover:

- Requested limits below 1 or above remaining quota fail before a run/provider call.
- A 200-record default run accepts at most 200 new unique candidates even when the provider emits more.
- Duplicate candidates associate once, do not consume quota twice, and still appear once in export.
- Existing businesses in a new run are enriched when the current run lacks an enrichment checkpoint.
- Individual enrichment errors are persisted and do not fail the run.
- `blocked` and `failed` provider results preserve and export accepted partial records with matching run status.
- Keyboard interruption marks `partial`, persists work, and attempts partial export.
- Resume ingests durable provider rows through the provider, safely replays duplicates, enriches only pending associations, and finishes the same run ID.
- Resume refuses a completed run and accepts only additional new records permitted by the current day's remaining quota.
- A resumed run whose persisted `new_unique_count` already equals `requested_limit` replays durable rows, enriches, and exports without starting another provider acquisition attempt.
- Export failure leaves records durable and sets a readable service error without discarding the provider/enrichment status.
- Progress events report acquisition candidates, newly accepted records, enrichment completion, and final export paths without exposing page bodies or URL credentials.

- [ ] **Step 2: Run service tests and verify RED**

Run: `python -m pytest tests/test_service.py -q`

Expected: import fails for `mapslead.service`.

- [ ] **Step 3: Implement the orchestration state machine**

Define:

```python
class MapsLeadService:
    def scrape(self, business: str, location: str, limit: int, now: datetime,
               progress: ProgressSink) -> RunOutcome: ...
    def resume(self, run_id: str, now: datetime, progress: ProgressSink) -> RunOutcome: ...
```

Validate the limit against repository allowance before `create_run`. Pass a sink to the provider that stops accepting new businesses once the run's new-unique limit is reached but continues treating replayed duplicates safely. After acquisition, iterate `pending_enrichment(run_id)`, checkpoint each result, export snapshots, and apply final state precedence: `blocked` or `failed` from provider, `partial` on interruption, otherwise `completed`. Preserve the acquisition state when export alone fails.

For a new run call only `provider.acquire(request, sink)`. For resume, first call `provider.replay(request, sink)` to ingest all durable attempts without Docker; then compute run capacity as `requested_limit - repository.new_unique_count_for_run(run_id)`. Call `provider.acquire(request, sink)` once for a new attempt only when this capacity and the current daily allowance are both positive and the prior state was not completed. A result with `interrupted=True` deterministically produces run status `partial`, triggers partial export, and returns control to the CLI without a second exception path.

- [ ] **Step 4: Verify service GREEN**

Run: `python -m pytest tests/test_service.py -q`

Expected: all service tests pass.

Run: `python -m ruff check src/mapslead/service.py tests/test_service.py && python -m mypy src/mapslead`

Expected: exit code 0.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/mapslead/service.py tests/test_service.py
git commit -m "feat: orchestrate resumable MapsLead runs"
```

---

### Task 7: CLI, prerequisite guidance, README, and end-to-end offline test

**Files:**
- Create: `src/mapslead/cli.py`
- Create: `tests/test_cli.py`
- Create: `README.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `Settings`, `Repository`, provider, enricher, exporter, service
- Produces: Typer `app` with `scrape`, `quota`, `resume`, and `export` commands

- [ ] **Step 1: Write failing CLI tests**

Use `typer.testing.CliRunner`, dependency injection through `build_service(settings)`, and temporary paths. Cover:

```python
def test_scrape_command_passes_business_location_and_limit(fake_service):
    result = runner.invoke(app, ["scrape", "--business", "dentists", "--location", "HCMC", "--limit", "25"])
    assert result.exit_code == 0
    assert "results.csv" in result.stdout
    assert fake_service.scrape_call == ("dentists", "HCMC", 25)


def test_quota_command_reports_used_and_remaining(fake_repository):
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0
    assert "Used today: 125" in result.stdout
    assert "Remaining: 875" in result.stdout
```

Also cover default limit 200, quota-exhausted and invalid-argument exit codes, blocked/partial messages with export paths, resume by run ID, export regeneration, Docker-not-installed guidance, Docker-image-missing guidance, Scrapling-import failure, Chromium-executable-missing guidance, and no traceback for expected operator errors. Every prerequisite failure must occur before `Repository.create_run`.

Assert that service progress events render concise acquisition/enrichment counts and final export paths, and that CLI `--data-dir` and `--export-dir` override `MAPSLEAD_DATA_DIR` and `MAPSLEAD_EXPORT_DIR` without affecting timezone or daily quota.

Add one offline end-to-end test using the real service/repository/exporter, fake provider, and fixture fetcher. Assert the final CSV and JSON contents, quota count, completed status, and stable re-export.

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `python -m pytest tests/test_cli.py -q`

Expected: import fails for `mapslead.cli`.

- [ ] **Step 3: Implement CLI and operator documentation**

Create Typer commands:

```text
mapslead scrape --business TEXT --location TEXT [--limit INTEGER=200]
mapslead quota
mapslead resume RUN_ID
mapslead export --run-id RUN_ID
```

Expose `--data-dir PATH` and `--export-dir PATH` as Typer application-level options shared by every command. Defaults come from `Settings.from_env()` and then fall back to `./data` and `./exports`.

Before creating a run, check `docker version` and `docker image inspect gosom/google-maps-scraper` with argument-list subprocess calls. Import `scrapling.fetchers.Fetcher`, obtain Chromium's configured executable path through `playwright.sync_api.sync_playwright()`, and verify that path exists without launching a browser. Report exact remediation:

```text
Docker is unavailable. Install and start Docker, then retry.
Provider image is missing. Run: docker pull gosom/google-maps-scraper
Scrapling is unavailable. Reinstall MapsLead with: pip install -e .
Chromium is missing. Run: playwright install chromium
```

Document Python 3.12, virtual-environment installation, `pip install -e '.[dev]'`, `playwright install chromium`, Docker image setup, first run, quota semantics, data/export locations, resume/export commands, no-CAPTCHA-bypass behavior, testing commands, and the explicit live smoke procedure capped at five requested results.

Ignore `.venv/`, `data/`, `exports/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `__pycache__/`, coverage files, and build artifacts.

- [ ] **Step 4: Run the full verification suite**

Run: `python -m pytest -q`

Expected: all tests pass and live network/Docker are not used.

Run: `python -m ruff check .`

Expected: exit code 0.

Run: `python -m mypy src/mapslead`

Expected: exit code 0.

Run: `python -m build`

Expected: wheel and source distribution build successfully.

- [ ] **Step 5: Commit Task 7**

```bash
git add .gitignore README.md src/mapslead/cli.py tests/test_cli.py
git commit -m "feat: deliver the MapsLead command line workflow"
```

---

## Final Verification

- [ ] Run `python -m pytest -q` and confirm zero failures.
- [ ] Run `python -m ruff check .` and confirm zero errors.
- [ ] Run `python -m mypy src/mapslead` and confirm zero errors.
- [ ] Run `python -m build` and confirm both distribution artifacts are created.
- [ ] Run `mapslead --help`, `mapslead scrape --help`, `mapslead quota`, `mapslead resume --help`, and `mapslead export --help` against a temporary data directory.
- [ ] Confirm `git status --short` contains no generated data, exports, caches, or provider output.
- [ ] Dispatch the Superpowers whole-branch code reviewer with the complete branch diff and resolve all Critical/Important findings.
