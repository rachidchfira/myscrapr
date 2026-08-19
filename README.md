# MapsLead

MapsLead is a local CLI for collecting Google Maps lead candidates, enriching business websites, and exporting each run as CSV and JSON.

## Requirements

- Python 3.12
- Docker Desktop or another local Docker runtime

## Install

Create a virtual environment, install the project in editable mode with dev tools, and pull the provider image:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker pull gosom/google-maps-scraper
```

## Commands

Global options:

- `--data-dir PATH` overrides `MAPSLEAD_DATA_DIR` for the SQLite database and provider work files.
- `--export-dir PATH` overrides `MAPSLEAD_EXPORT_DIR` for per-run exports.

Main commands:

- `mapslead scrape --business TEXT --location TEXT [--limit INTEGER]`
- `mapslead quota`
- `mapslead resume RUN_ID`
- `mapslead export --run-id RUN_ID`

## First Run

```bash
.venv/bin/mapslead --data-dir ./data --export-dir ./exports scrape \
  --business "dentists" \
  --location "Ho Chi Minh City" \
  --limit 25
```

Before `scrape`, and before `resume` for an existing resumable run, MapsLead checks the actual acquisition prerequisites and prints these exact remediations when needed:

- `Docker is unavailable. Install and start Docker, then retry.`
- `Provider image is missing. Run: docker pull gosom/google-maps-scraper`

`resume` validates run existence and status first. Missing, completed, and running runs fail immediately with an accurate no-traceback message and do not probe Docker.

Progress output stays concise:

- acquisition updates show accepted candidate and new-unique counts
- enrichment updates show completed website enrichments
- export updates print the final CSV and JSON paths

## Quota And Storage

- Daily quota is fixed at `1000` new unique records per local day in the configured timezone.
- The default requested limit is `200`.
- SQLite state lives under `data/mapslead.sqlite3` unless you override `--data-dir` or `MAPSLEAD_DATA_DIR`.
- Per-run exports live under `exports/<run-id>/results.csv` and `exports/<run-id>/results.json` unless you override `--export-dir` or `MAPSLEAD_EXPORT_DIR`.

Check quota at any time:

```bash
.venv/bin/mapslead --data-dir ./data quota
```

Resume a blocked, partial, or failed run:

```bash
.venv/bin/mapslead --data-dir ./data --export-dir ./exports resume <run-id>
```

Regenerate exports for an existing run:

```bash
.venv/bin/mapslead --data-dir ./data --export-dir ./exports export --run-id <run-id>
```

## Website Enrichment

- Enrichment uses the built-in SSRF-safe direct HTTP transport behind the existing `ScraplingPageFetcher` compatibility name.
- Only same-registrable-domain pages are followed.
- `robots.txt` is respected.
- MapsLead does not attempt CAPTCHA bypass, proxy rotation, or anti-bot circumvention.

## Verified Commands

These commands were verified in this repository on August 19, 2026:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/mapslead
.venv/bin/python -m build
.venv/bin/mapslead --help
.venv/bin/mapslead scrape --help
.venv/bin/mapslead --data-dir /tmp/mapslead-help-data quota
.venv/bin/mapslead resume --help
.venv/bin/mapslead export --help
```

## Live Smoke Procedure

This is the manual smoke path for a real networked run. Keep it capped at five requested results:

1. Ensure Docker is running and the provider image is present: `docker pull gosom/google-maps-scraper`
2. Start a small run: `.venv/bin/mapslead --data-dir ./smoke-data --export-dir ./smoke-exports scrape --business "dentists" --location "Ho Chi Minh City" --limit 5`
3. Confirm the CLI prints a run ID plus `results.csv` and `results.json` paths.
4. Inspect `./smoke-exports/<run-id>/results.csv` and `./smoke-exports/<run-id>/results.json`.
5. Re-run `.venv/bin/mapslead --data-dir ./smoke-data --export-dir ./smoke-exports quota` and confirm `Used today: 5`.

## Test Commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/mapslead
.venv/bin/python -m build
```
