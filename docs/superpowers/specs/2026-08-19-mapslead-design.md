# MapsLead Hybrid Scraper Design

## Purpose

MapsLead is a local command-line tool for personal research. It finds businesses from a Google Maps business-type and location query, enriches those businesses from their public websites, and exports the resulting records as CSV and JSON.

The first version is manually launched, runs entirely on the user's computer, and accepts at most 1,000 new unique business records per calendar day in the `Asia/Ho_Chi_Minh` timezone.

## Goals

- Accept a business type and location as the primary search inputs.
- Acquire Google Maps business records through the self-hosted `gosom/google-maps-scraper` Docker image.
- Enrich each business from its public website with email addresses and social-profile links.
- Store run state and business records locally in SQLite.
- Resume interrupted runs without counting or exporting duplicate businesses twice.
- Export each run's results to both CSV and JSON.
- Stop safely when Google Maps presents a CAPTCHA or blocking condition.
- Remain useful without paid APIs, proxy subscriptions, hosted databases, or LLM services.

## Non-goals

- Running as a hosted service or multi-user application.
- Automatic daily scheduling.
- CAPTCHA solving, anti-bot circumvention, proxy rotation, or account automation.
- Scraping pages behind authentication or paywalls.
- Sending outreach messages or integrating with a CRM.
- Providing a graphical interface.
- General-purpose crawling beyond the websites associated with acquired business records.

## User Interface

The package exposes a `mapslead` command. The primary workflow is:

```bash
mapslead scrape \
  --business "dentists" \
  --location "Ho Chi Minh City" \
  --limit 200
```

The requested run limit must be between 1 and the remaining daily allowance. The default run limit is 200. The command displays the requested limit, remaining daily allowance, acquisition status, enrichment progress, and final export paths.

Supporting commands are:

```bash
mapslead quota
mapslead resume RUN_ID
mapslead export --run-id RUN_ID
```

`mapslead quota` reports today's accepted-record count and remaining allowance. `mapslead resume` continues an interrupted, blocked, or failed run from its stored checkpoints; it may accept new businesses only when daily allowance remains. `mapslead export` regenerates CSV and JSON files for an existing run without consuming quota.

## Architecture

The application is a Python 3.12 package with five focused components:

1. **CLI controller** validates arguments, coordinates a run, reports progress, and maps expected failures to readable exit messages.
2. **Maps provider** invokes the locally available `gosom/google-maps-scraper` Docker image, writes its raw output to a durable run-specific directory, and translates provider records into the internal business model.
3. **Website enricher** uses Scrapling to retrieve a business homepage and selected same-domain pages, then extracts emails and supported social links.
4. **SQLite repository** owns schema creation, run checkpoints, daily quota accounting, deduplication, and stored results.
5. **Exporter** serializes a completed or interrupted run's accepted records into deterministic CSV and JSON files.

External-process execution and website fetching sit behind Python protocols so unit tests can exercise orchestration without Docker or live network access.

## Data Flow

1. The CLI validates `business`, `location`, and `limit`.
2. The repository computes the remaining allowance for the current `Asia/Ho_Chi_Minh` calendar day.
3. The repository creates a run with a stable run ID and `running` status.
4. The Maps provider launches one bounded local acquisition job. The application accepts no more new unique businesses than the smaller of the requested limit and remaining allowance.
5. Each normalized candidate is deduplicated before insertion.
6. A newly accepted unique business atomically consumes one unit of the daily allowance.
7. Existing businesses are associated with the run but do not consume allowance.
8. The website enricher processes every business associated with the active run that has a website and no completed enrichment checkpoint for that run, including reused businesses that did not consume quota.
9. The exporter writes all records associated with the run, including records with partial enrichment.
10. The run ends as `completed`, `partial`, `blocked`, or `failed`, retaining enough state to export or resume it.

Quota checking and insertion occur in the same SQLite transaction. Concurrent invocations therefore cannot collectively exceed 1,000 newly accepted unique records for a day.

## Business Record

Each exported record is an immutable run snapshot and contains:

- `place_id`
- `name`
- `category`
- `address`
- `phone`
- `website`
- `rating`
- `review_count`
- `google_maps_url`
- `emails`
- `facebook_url`
- `instagram_url`
- `linkedin_url`
- `x_url`
- `youtube_url`
- `business_type`
- `location_query`
- `first_seen_at`
- `last_seen_at`
- `enrichment_status`
- `enrichment_error`
- `run_id`

CSV represents multiple emails as a semicolon-separated, sorted list. JSON represents emails as a sorted array. Missing values are empty in CSV and `null` in JSON, except `emails`, which is an empty list.

## Deduplication and Quota Rules

Every accepted candidate must have a non-empty normalized name. Its primary deduplication key is a non-empty Google Place ID when available. When Place ID is absent, the first available fallback key is selected in this order: normalized name plus normalized address; normalized name plus normalized phone; or normalized name plus the registrable domain of the normalized website. A candidate that lacks both a Place ID and every complete fallback pair is rejected and does not consume quota.

Normalization trims surrounding whitespace, collapses internal whitespace, and applies Unicode case folding. Phone normalization retains digits and a leading plus sign. A record is considered new only when neither its Place ID nor selected fallback key matches an existing business. Alternative fallback keys derived from the same accepted record are stored as aliases so a later provider result can match through any complete pair.

Only a new unique business inserted successfully into SQLite consumes quota. Duplicate provider results, resumed records, failed candidates, exports, and enrichment retries do not consume quota.

The `--limit` value is a maximum number of new unique businesses to accept, not a guaranteed result count. A run can return fewer records when the provider has fewer matches, returns duplicates, is interrupted, or becomes blocked. Existing duplicate businesses associated with the run appear in its exports but do not reduce the new-record allowance.

The daily quota boundary uses `Asia/Ho_Chi_Minh`, regardless of the computer's current timezone configuration. The limit is fixed at 1,000 records in version 1 and is not overridable by command-line flags or environment variables.

## Google Maps Acquisition

Version 1 integrates the open-source `gosom/google-maps-scraper` provider through Docker. MapsLead constructs a query from the business type and location, mounts a durable directory at `./data/runs/<run-id>/provider`, and requests machine-readable output. Complete provider rows are validated and inserted individually, so every accepted candidate is durable before the next candidate is handled.

The provider runs at conservative concurrency. MapsLead does not configure proxies, CAPTCHA solvers, authenticated Google sessions, or bypass services. Provider stderr and exit status are retained in the run log without placing secrets in log messages.

Blocked-state detection is based on case-insensitive provider diagnostics matching a maintained set of explicit signals: `captcha`, `unusual traffic`, `too many requests`, `rate limit`, HTTP status `429`, or a provider-declared blocked status. A blocked signal takes precedence over a generic non-zero provider exit. When detected, MapsLead stops the provider and marks the run `blocked`. A non-zero exit without a blocked signal is `failed`; a successful empty result is `completed`. Records already accepted remain stored and exportable.

The provider interface is intentionally replaceable so a future permitted source or official API can be added without changing quota, enrichment, or export behavior.

## Website Enrichment

For each business associated with the active run that has an HTTP or HTTPS website and no completed enrichment checkpoint for that run, including reused non-quota businesses, the enricher:

1. Fetches the homepage.
2. Identifies same-domain links whose path or anchor text indicates Contact, About, or Team content.
3. Selects at most three additional pages, preferring Contact, then About, then Team.
4. Fetches at most four total pages for the business.
5. Extracts visible and mailto email addresses and links for Facebook, Instagram, LinkedIn, X/Twitter, and YouTube.

Redirects may remain on the original registrable domain or its subdomains. A redirect to a different registrable domain is not followed for enrichment. Non-HTTP schemes, downloads, authentication pages, and URLs outside the selected business domain are rejected.

Email and social results are normalized, deduplicated, sorted, and stored after each business. A fetch failure is recorded on that business and does not fail the overall run. The enricher uses one active request per domain and conservative delays. It obeys `robots.txt` where the target publishes applicable rules.

## Persistence and Resumption

SQLite is stored under a configurable data directory that defaults to `./data`. The database contains businesses, identity aliases, runs, run-business associations, daily quota counters, and enrichment checkpoints. The run-business table has a unique `(run_id, business_id)` constraint, so provider replay cannot create duplicate rows in one run.

The canonical business row holds the latest known identity and contact data. When a business is associated with a run, the run-business row stores that run's business type, location query, first-seen value, current sighting time, Maps fields, and enrichment result as an immutable export snapshot. Enrichment updates the snapshot for the active run and the canonical latest-known record; later runs never alter an older run's snapshot. Regenerating an old export therefore produces the same business data and ordering as its original completed export.

Starting the same business-type and location query creates a new run. It can reuse existing business records without consuming quota and may retry incomplete enrichment for the new run. An interrupted run can be resumed explicitly by its run ID. The external Maps provider itself is not assumed to support process-level checkpoints: resume first ingests any complete durable provider rows not yet recorded, then launches a fresh bounded provider invocation for the same query when acquisition was incomplete. SQLite identity aliases, the unique run-business constraint, and quota transactions make replay safe. Website enrichment resumes from per-business checkpoints.

Database writes use transactions and foreign-key enforcement. Schema migrations are versioned from the first release.

## Outputs

Exports default to `./exports/<run-id>/results.csv` and `results.json`. A temporary file is written in the destination directory and atomically renamed after serialization, preventing a crash from replacing a valid export with a truncated one.

Ordering is deterministic: normalized business name, normalized address, then internal business ID. Existing export files for the same run are replaced only after the new complete files are ready.

Generated databases, raw provider output, logs, temporary files, and exports are ignored by Git.

## Error Handling

Expected failures produce concise messages and non-zero exit codes:

- Invalid arguments: no run is created.
- Docker unavailable or provider image missing: the run fails with setup guidance.
- Provider blocked: the run is marked `blocked`, and partial results are exported.
- Provider crash or malformed output: the run is marked `failed`, retaining diagnostic metadata and accepted records.
- Individual website failure: the business receives an enrichment error and the run continues.
- Export failure: stored records remain intact and the command reports the destination error.
- Quota exhausted: no new acquisition starts; existing runs remain exportable.

Keyboard interruption requests provider termination, persists the current checkpoint, exports accepted records when possible, and marks the run `partial`.

## Security and Privacy

- No credentials are required for version 1.
- Subprocess arguments are constructed as an argument list and never passed through a shell.
- User inputs are data, not command fragments or file paths.
- Provider output is parsed with explicit field validation and size bounds.
- Website fetching blocks local, loopback, link-local, private-network, and non-HTTP destinations to reduce server-side request-forgery risk.
- Redirects are revalidated before following.
- Logs avoid full page content and redact URL credentials if encountered.
- All collected data and exports remain local unless the user moves them elsewhere.

## Testing Strategy

Development follows test-driven development. Unit tests use saved, minimal fixtures rather than live Google Maps or arbitrary websites.

Required coverage includes:

- Argument and limit validation.
- Atomic 1,000-record daily quota enforcement, including concurrent repository calls.
- Place-ID and fallback-key deduplication.
- Provider command construction, blocked-state detection, malformed output, and interruption handling.
- Same-domain page selection and redirect validation.
- Email and social-link normalization.
- Partial enrichment and resumable checkpoints.
- Deterministic CSV/JSON serialization and atomic replacement.
- End-to-end orchestration using a fake Maps provider and a local fixture HTTP server.

A documented developer-only smoke-test procedure may exercise Docker and the live Maps provider with a very small result count. It is not part of the public CLI, is excluded from the default automated test suite, and never runs in CI without an explicit environment flag.

## Packaging and Operations

The project uses a `pyproject.toml` package definition, a `src/` layout, pytest, Ruff, and type checking. The README documents Python, Docker, browser requirements, installation, first run, quota behavior, data locations, resumption, exports, and the live-smoke-test warning.

The CLI checks prerequisites before creating a provider job and gives exact remediation commands when Docker, the provider image, or Scrapling browser dependencies are unavailable.
