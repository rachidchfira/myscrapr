# MapsLead Nationwide Campaigns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit nationwide campaigns that isolate niches, reuse unchanged successful website enrichment, attach legacy runs, and export one deduplicated master CSV/JSON.

**Architecture:** Extend the existing SQLite repository with campaign, membership, discovery, and enrichment-cache state while keeping canonical business identity and quota global. The service remains the only orchestration boundary; a focused campaign exporter produces atomic master files, and a Typer sub-application exposes campaign operations without weakening existing run commands.

**Tech Stack:** Python 3.12, SQLite, Pydantic v2, Typer, pytest, Ruff, mypy, `build`.

**Spec:** `docs/superpowers/specs/2026-08-19-mapslead-campaigns-design.md`

## Global Constraints

- Campaign membership is explicit; a run belongs to at most one campaign, and legacy runs may remain unassigned.
- Campaign slug is 1-64 lowercase ASCII letters/digits separated by single hyphens, with no leading or trailing hyphen.
- Campaign business type is immutable and compared through existing Unicode/whitespace normalization.
- Canonical businesses, identity aliases, and the 1,000-new-unique-record daily quota remain global.
- Address alone is never a business identity key.
- Successful enrichment is reused indefinitely only while the normalized website URL is unchanged.
- Failed, skipped, robots-disallowed, and unsafe enrichment never replace a successful cache entry.
- `--refresh-enrichment` is persisted on the run and bypasses cache reuse for that run.
- Campaign master exports contain one row per campaign/business membership and use atomic paired CSV/JSON replacement.
- All automated tests are offline; no test contacts Docker, Google Maps, or a business website.
- Existing non-campaign CLI behavior and the current database must migrate without data loss.

---

### Task 1: Campaign domain contracts, migration, CRUD, attachment, and membership

**Files:**
- Modify: `src/mapslead/models.py`
- Modify: `src/mapslead/errors.py`
- Modify: `src/mapslead/ports.py`
- Modify: `src/mapslead/repository.py`
- Create: `tests/test_campaign_repository.py`

**Interfaces:**
- Consumes: `normalize_text`, existing `RunRecord`, `RunSnapshot`, canonical business and quota transactions.
- Produces: `CampaignRecord`, `CampaignSnapshot`, `CampaignStatus`, `EnrichmentCacheEntry`; extended `RunRecord`; repository campaign methods and schema version 2.

- [ ] **Step 1: Write failing migration and campaign validation tests**

Create a real schema-version-1 SQLite fixture with one completed run/business snapshot. Test that `initialize()` migrates it to version 2 without changing the run, quota, canonical business, or snapshot. Add literal slug cases:

```python
@pytest.mark.parametrize("slug", ["Vietnam", "-vietnam", "vietnam-", "viet--nam", "viet_nam", "a" * 65])
def test_create_campaign_rejects_unsafe_slug(repository, slug, now):
    with pytest.raises(InvalidCampaignError):
        repository.create_campaign(slug, "dentists", now)

def test_v1_database_migrates_without_losing_existing_run(v1_repository, existing_run_id):
    v1_repository.initialize()
    assert v1_repository.get_run(existing_run_id).status is RunStatus.COMPLETED
    assert len(v1_repository.snapshots_for_run(existing_run_id)) == 1
```

- [ ] **Step 2: Run the new repository tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_campaign_repository.py -q`

Expected: collection fails because campaign models/errors/methods do not exist.

- [ ] **Step 3: Add frozen domain models and errors**

Add these exact public shapes:

```python
class CampaignRecord(FrozenModel):
    slug: str
    business_type: str
    created_at: datetime

class EnrichmentCacheEntry(FrozenModel):
    business_id: int
    normalized_website: str
    result: EnrichmentResult
    completed_at: datetime

class CampaignSnapshot(FrozenModel):
    business_id: int
    campaign_id: str
    discovered_in: tuple[str, ...]
    name: str
    business_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    place_id: str | None = None
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    review_count: int | None = None
    google_maps_url: str | None = None
    emails: tuple[str, ...] = ()
    facebook_url: str | None = None
    instagram_url: str | None = None
    linkedin_url: str | None = None
    x_url: str | None = None
    youtube_url: str | None = None
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    enrichment_error: str | None = None

class CampaignStatus(FrozenModel):
    campaign: CampaignRecord
    run_count: int = Field(ge=0)
    business_count: int = Field(ge=0)
    discovered_in: tuple[str, ...] = ()
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
```

Extend `RunRecord` with `campaign_slug: str | None = None` and `refresh_enrichment: bool = False`. Add `CampaignError`, `InvalidCampaignError`, `CampaignNotFoundError`, `CampaignBusinessTypeError`, and `CampaignRunAssignmentError` under `MapsLeadError`.

- [ ] **Step 4: Implement schema version 2 and idempotent migration**

Set `_SCHEMA_VERSION = 2`. New databases create the three campaign tables, `business_enrichment_cache`, and `runs.refresh_enrichment INTEGER NOT NULL DEFAULT 0`. Existing version-1 databases migrate inside one transaction with:

```sql
ALTER TABLE runs ADD COLUMN refresh_enrichment INTEGER NOT NULL DEFAULT 0;
CREATE TABLE campaigns(...);
CREATE TABLE campaign_runs(... run_id TEXT NOT NULL UNIQUE ...);
CREATE TABLE campaign_businesses(... PRIMARY KEY(campaign_slug, business_id));
CREATE TABLE business_enrichment_cache(...);
UPDATE schema_version SET version = 2;
```

`initialize()` must accept version 1, apply the migration once, and reject versions greater than 2.

- [ ] **Step 5: Add campaign CRUD and atomic attachment tests**

Cover create/get, duplicate create failure, idempotent same-campaign attachment, missing run, normalized business-type mismatch, and rejection when a run is assigned to another campaign. The attachment success test must assert one campaign business despite duplicate provider sightings and unchanged daily quota.

```python
campaign = repository.create_campaign("vietnam-dentists", "dentists", now)
repository.attach_run(campaign.slug, run.id, now)
repository.attach_run(campaign.slug, run.id, now)
assert repository.campaign_for_run(run.id) == campaign
assert repository.campaign_status(campaign.slug).business_count == 1
assert repository.remaining_quota(now) == remaining_before_attach
```

- [ ] **Step 6: Implement campaign repository operations and automatic membership**

Extend `RepositoryPort` and `SQLiteRepository` with:

```python
def create_campaign(self, slug: str, business: str, now: datetime) -> CampaignRecord: ...
def get_campaign(self, slug: str) -> CampaignRecord: ...
def attach_run(self, slug: str, run_id: str, now: datetime) -> CampaignRecord: ...
def campaign_for_run(self, run_id: str) -> CampaignRecord | None: ...
def campaign_status(self, slug: str) -> CampaignStatus: ...
def campaign_snapshots(self, slug: str) -> Sequence[CampaignSnapshot]: ...
```

Extend `create_run(..., *, campaign_slug: str | None = None, refresh_enrichment: bool = False)`. When a campaign is supplied, validate its normalized business type and insert `campaign_runs` in the same transaction as `runs`. In `accept_candidate`, after creating the run snapshot, upsert `campaign_businesses` in the same `BEGIN IMMEDIATE` transaction using the run's campaign association.

For Task 1, `campaign_snapshots` merges latest canonical business fields with the most recent campaign run snapshot and derives sorted distinct `discovered_in` values from associated runs. Task 2 then adds matching successful-cache precedence after URL normalization and cache methods exist. `campaign_status` counts a business with no HTTP(S) website as skipped; remaining non-completed/non-failed memberships are pending.

- [ ] **Step 7: Verify Task 1 and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_campaign_repository.py tests/test_repository.py tests/test_models.py -q
.venv/bin/python -m ruff check src/mapslead/models.py src/mapslead/errors.py src/mapslead/ports.py src/mapslead/repository.py tests/test_campaign_repository.py
.venv/bin/python -m mypy src/mapslead
```

Commit:

```bash
git add src/mapslead/models.py src/mapslead/errors.py src/mapslead/ports.py src/mapslead/repository.py tests/test_campaign_repository.py
git commit -m "feat: persist isolated campaign membership"
```

---

### Task 2: Enrichment cache reuse and persisted refresh behavior

**Files:**
- Modify: `src/mapslead/normalize.py`
- Modify: `src/mapslead/ports.py`
- Modify: `src/mapslead/repository.py`
- Modify: `src/mapslead/service.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_campaign_repository.py`

**Interfaces:**
- Consumes: Task 1 `EnrichmentCacheEntry`, extended `RunRecord`, campaign attachment and snapshots.
- Produces: `normalize_website_url`, cache repository methods, campaign-aware `MapsLeadService.scrape` and deterministic resume reuse.

- [ ] **Step 1: Write failing normalization and repository cache tests**

Use literal equivalents showing that scheme/host case, default ports, trailing root slash, and fragments do not cause false misses, while a different path/query remains distinct:

```python
assert normalize_website_url("HTTPS://Example.COM:443/#team") == "https://example.com/"
assert normalize_website_url("https://example.com/contact?lang=en") == "https://example.com/contact?lang=en"
```

Test `save_cached_enrichment` accepts only `EnrichmentStatus.COMPLETED`, returns a match only for the same normalized website, and never lets a failed result replace a previous success.

- [ ] **Step 2: Verify cache tests RED**

Run: `.venv/bin/python -m pytest tests/test_campaign_repository.py -q -k cache`

Expected: FAIL because URL normalization and cache methods do not exist.

- [ ] **Step 3: Implement URL normalization and cache methods**

Add:

```python
def normalize_website_url(value: str | None) -> str | None: ...

def cached_enrichment(self, business_id: int, website: str | None) -> EnrichmentCacheEntry | None: ...
def save_cached_enrichment(
    self, business_id: int, website: str, result: EnrichmentResult, now: datetime
) -> None: ...
```

Normalize HTTP(S) scheme/hostname casing, remove fragments, remove default ports, preserve path/query, and return `None` for missing/non-HTTP URLs. Cache lookup must compare the normalized URL exactly. Cache writes reject non-completed results.

- [ ] **Step 4: Write failing service tests for reuse, invalidation, refresh, and resume**

Extend fake repository/enricher coverage so observable fetch call counts prove behavior:

```python
service.scrape("dentists", "Hanoi", 10, now, progress, campaign_slug="vietnam-dentists")
assert enricher.websites == []
assert repository.snapshots_for_run(run_id)[0].emails == ("cached@example.com",)
```

Separate tests must prove a changed website calls the enricher, `refresh_enrichment=True` calls it despite a matching cache, successful fetch updates the cache, failed fetch preserves the prior successful cache, and resume uses the persisted run refresh flag rather than a new CLI value.

- [ ] **Step 5: Implement campaign-aware scrape and cache orchestration**

Extend service signature:

```python
def scrape(
    self,
    business: str,
    location: str,
    limit: int,
    now: datetime,
    progress: ProgressSink,
    *,
    campaign_slug: str | None = None,
    refresh_enrichment: bool = False,
) -> RunOutcome: ...
```

Pass both fields to `create_run`. In `_run_pending_enrichment`, load the run once. For each snapshot, reuse a matching cache unless `run.refresh_enrichment`; copy it through `save_enrichment`, emit `ProgressEvent(kind="enrichment_reused", message="Reused cached enrichment.")`, and never call the enricher. After a newly fetched completed result, call `save_enrichment` then `save_cached_enrichment`. Resume reads the persisted fields from `RunRecord` and cannot change them.

During Task 1 attachment, seed cache entries from attached snapshots with completed enrichment and a canonical-matching normalized website.

- [ ] **Step 6: Verify Task 2 and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_service.py tests/test_campaign_repository.py tests/test_models.py -q
.venv/bin/python -m ruff check src/mapslead/normalize.py src/mapslead/ports.py src/mapslead/repository.py src/mapslead/service.py tests/test_service.py tests/test_campaign_repository.py
.venv/bin/python -m mypy src/mapslead
```

Commit:

```bash
git add src/mapslead/normalize.py src/mapslead/ports.py src/mapslead/repository.py src/mapslead/service.py tests/test_service.py tests/test_campaign_repository.py
git commit -m "feat: reuse campaign website enrichment"
```

---

### Task 3: Deterministic atomic campaign master exports

**Files:**
- Create: `src/mapslead/campaign_exporter.py`
- Create: `tests/test_campaign_exporter.py`
- Modify: `src/mapslead/ports.py`

**Interfaces:**
- Consumes: Task 1 `CampaignSnapshot`, `RepositoryPort.campaign_snapshots`, `Settings.export_dir`.
- Produces: `CampaignExporter.export_campaign(slug: str) -> ExportPaths` and `CampaignExporterPort`.

- [ ] **Step 1: Write failing golden export tests**

Use literal snapshots with duplicate discoveries across Hanoi/HCMC. Assert one row per business, normalized name/address/business-ID sorting, exact 22-field order from the spec, sorted semicolon CSV collections, sorted JSON arrays, ISO timestamps, null JSON scalars, and paths under `exports/campaigns/vietnam-dentists/`.

```python
paths = exporter.export_campaign("vietnam-dentists")
assert list(csv.DictReader(paths.csv_path.open()))[0]["discovered_in"] == "Hanoi;Ho Chi Minh City"
assert json.loads(paths.json_path.read_text())[0]["campaign_id"] == "vietnam-dentists"
```

- [ ] **Step 2: Verify exporter RED**

Run: `.venv/bin/python -m pytest tests/test_campaign_exporter.py -q`

Expected: import fails for `mapslead.campaign_exporter`.

- [ ] **Step 3: Implement campaign exporter**

Create a focused exporter rather than adding campaign branching to `Exporter`. Reuse or extract the existing atomic write/replace helpers without weakening run export tests. Validate the slug as a single safe component and enforce resolved containment under `<export-dir>/campaigns` before creating directories.

The exact field order is:

```python
CAMPAIGN_CSV_FIELDS = (
    "place_id", "name", "category", "address", "phone", "website",
    "rating", "review_count", "google_maps_url", "emails", "facebook_url",
    "instagram_url", "linkedin_url", "x_url", "youtube_url", "business_type",
    "first_seen_at", "last_seen_at", "enrichment_status", "enrichment_error",
    "campaign_id", "discovered_in",
)
```

On serialization or either replacement failure, leave an existing valid pair unchanged and remove this attempt's temp/backup files.

- [ ] **Step 4: Add failure, containment, and stable re-export tests**

Cover JSON serialization failure, second destination replacement failure, absolute/dot/separator/symlink slug escapes, successful pair replacement, no temporary files, and byte-identical re-export for unchanged data.

- [ ] **Step 5: Verify Task 3 and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_campaign_exporter.py tests/test_exporter.py -q
.venv/bin/python -m ruff check src/mapslead/campaign_exporter.py src/mapslead/ports.py tests/test_campaign_exporter.py
.venv/bin/python -m mypy src/mapslead
```

Commit:

```bash
git add src/mapslead/campaign_exporter.py src/mapslead/ports.py tests/test_campaign_exporter.py
git commit -m "feat: export deduplicated campaign masters"
```

---

### Task 4: Campaign CLI, status, documentation, and offline end-to-end flow

**Files:**
- Modify: `src/mapslead/cli.py`
- Modify: `tests/test_cli.py`
- Create: `tests/test_campaign_e2e.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1-3 campaign repository/service/exporter APIs.
- Produces: Typer `campaign` sub-application and campaign-aware `scrape` command.

- [ ] **Step 1: Write failing CLI contract tests**

Register a Typer sub-application and cover:

```text
mapslead campaign create SLUG --business TEXT
mapslead campaign attach-run SLUG RUN_ID
mapslead campaign status SLUG
mapslead campaign export SLUG
mapslead scrape --campaign SLUG --location TEXT --limit INTEGER [--refresh-enrichment]
```

Tests assert exactly one of `--business` and `--campaign`, campaign business-type lookup before Docker checks, no Docker check for create/attach/status/export, accurate no-traceback errors, and unchanged legacy scrape behavior.

- [ ] **Step 2: Verify CLI RED**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q -k campaign`

Expected: FAIL because the sub-application and options are absent.

- [ ] **Step 3: Wire campaign runtime and commands**

Extend `AppRuntime` with `campaign_exporter`. Build it from the shared repository/settings. Add:

```python
campaign_app = typer.Typer(no_args_is_help=True)
app.add_typer(campaign_app, name="campaign")
```

`campaign create` uses `clock.now()`. `attach-run` is idempotent for the same campaign. `status` renders counts, sorted locations, global quota, and existing export paths. `campaign export` calls only `CampaignExporter`. Campaign `scrape` obtains the locked business type, checks the requested quota before Docker, then delegates to `MapsLeadService.scrape(..., campaign_slug=..., refresh_enrichment=...)`.

- [ ] **Step 4: Add the offline national-flow test**

With real SQLite/service/exporters and fake provider/enricher:

1. Create `vietnam-dentists`.
2. Create and enrich an HCMC legacy run.
3. Attach it and assert cache seeding.
4. Scrape Hanoi with one duplicate and one new business.
5. Assert duplicate quota is not consumed and cached website is not fetched.
6. Export the campaign and assert two unique rows with correct `discovered_in`.
7. Re-export and assert byte-identical files.
8. Create `vietnam-restaurants` and prove its export contains none of the dentists.

- [ ] **Step 5: Update README with exact operator workflow**

Document campaign isolation, unchanged global quota, cache reuse, master export paths, and these commands using the installed `.venv/bin/mapslead` entrypoint:

```bash
.venv/bin/mapslead campaign create vietnam-dentists --business dentists
.venv/bin/mapslead campaign attach-run vietnam-dentists 6f8d2ee1d37b44d7be6ce2413c0da825
.venv/bin/mapslead scrape --campaign vietnam-dentists --location Hanoi --limit 300
.venv/bin/mapslead campaign status vietnam-dentists
.venv/bin/mapslead campaign export vietnam-dentists
```

State that `data/` and `exports/` are local and not pushed to GitHub.

- [ ] **Step 6: Run full verification and commit**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/mapslead
.venv/bin/python -m build
.venv/bin/mapslead campaign --help
.venv/bin/mapslead scrape --help
```

Commit:

```bash
git add src/mapslead/cli.py tests/test_cli.py tests/test_campaign_e2e.py README.md
git commit -m "feat: deliver nationwide campaign workflow"
```

- [ ] **Step 7: Verify the approved existing-run attachment locally**

After all automated checks pass, run against the operator's existing local database:

```bash
.venv/bin/mapslead campaign create vietnam-dentists --business dentists
.venv/bin/mapslead campaign attach-run vietnam-dentists 6f8d2ee1d37b44d7be6ce2413c0da825
.venv/bin/mapslead campaign status vietnam-dentists
.venv/bin/mapslead campaign export vietnam-dentists
```

This step is authorized by the approved design. It must not call Docker or websites, consume quota, or modify the legacy run snapshot. If the campaign already exists or the same run is already attached, continue idempotently. Do not start the Hanoi scrape automatically.

---

## Final Verification

- [ ] Confirm the schema migrated the user's existing database to version 2 with the five-record HCMC run intact.
- [ ] Confirm `vietnam-dentists` contains the existing run and exactly its unique businesses before new city scrapes.
- [ ] Confirm campaign master CSV/JSON exist under `exports/campaigns/vietnam-dentists/`.
- [ ] Run `.venv/bin/python -m pytest -q` with zero failures.
- [ ] Run `.venv/bin/python -m ruff check .` with zero errors.
- [ ] Run `.venv/bin/python -m mypy src/mapslead` with zero errors.
- [ ] Run `.venv/bin/python -m build` and confirm wheel plus source distribution.
- [ ] Run a clean-wheel import/CLI smoke in a temporary Python 3.12 environment.
- [ ] Dispatch a whole-branch reviewer against the campaign spec and resolve all Critical/Important findings.
- [ ] Push the completed `main` branch to `origin` only after local merge and final verification.
