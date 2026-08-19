# MapsLead

MapsLead is a local CLI for collecting Google Maps lead candidates, enriching business websites, and exporting each run as CSV, JSON, and Excel. It also supports isolated nationwide campaigns so you can scrape one niche across many cities without mixing business types or exporting the same business twice.

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

- `mapslead scrape (--business TEXT | --campaign SLUG) --location TEXT [--location TEXT ...] [--query TEXT ...] [--language CODE] [--limit INTEGER] [--refresh-enrichment]`
- `mapslead quota`
- `mapslead resume RUN_ID`
- `mapslead export --run-id RUN_ID`
- `mapslead campaign create SLUG --business TEXT`
- `mapslead campaign attach-run SLUG RUN_ID`
- `mapslead campaign status SLUG`
- `mapslead campaign export SLUG`

## First Run

```bash
.venv/bin/mapslead --data-dir ./data --export-dir ./exports scrape \
  --business "dentists" \
  --location "Ho Chi Minh City" \
  --language en \
  --limit 25
```

Before `scrape`, and before `resume` for an existing resumable run, MapsLead checks the actual acquisition prerequisites and prints these exact remediations when needed:

- `Docker is unavailable. Install and start Docker, then retry.`
- `Provider image is missing. Run: docker pull gosom/google-maps-scraper`

`resume` validates run existence and status first. Only partial, blocked, and failed runs are resumable; missing, completed, and running runs fail immediately with an accurate no-traceback message and do not probe Docker.

Progress output stays concise:

- acquisition updates show accepted candidate and new-unique counts
- enrichment updates show completed website enrichments
- cached-enrichment updates show reuse without refetching a website
- export updates print the final CSV, JSON, and Excel paths

Google Maps acquisition scales Docker `-depth` from your requested limit at roughly 20 results per depth page, so larger limits can scroll past the first provider page instead of stopping around 20 rows.

## Campaign Workflow

Campaigns isolate one normalized business type, keep deduplication and quota global, and write one master CSV/JSON/Excel export set per campaign under `exports/campaigns/<slug>/`.

Use this workflow for the Vietnam dentist campaign:

```bash
.venv/bin/mapslead campaign create vietnam-dentists --business dentists
.venv/bin/mapslead campaign attach-run vietnam-dentists 6f8d2ee1d37b44d7be6ce2413c0da825
.venv/bin/mapslead scrape \
  --campaign vietnam-dentists \
  --query "nha khoa" \
  --query "phong kham nha khoa" \
  --location "Ha Noi" \
  --location "Quan 1, Ho Chi Minh City" \
  --location "Quan 3, Ho Chi Minh City" \
  --language vi \
  --limit 150
.venv/bin/mapslead campaign status vietnam-dentists
.venv/bin/mapslead campaign export vietnam-dentists
```

Rules that stay in force:

- Campaigns are explicit. A run is either attached to one campaign or to none.
- The daily `1000` new-unique quota stays global across campaign and non-campaign runs.
- Repeating `--query` and `--location` runs the full location-by-query batch in the order you provided. Each pair becomes its own run, and `--limit` applies per pair.
- MapsLead checks remaining daily quota before every pair, clamps a pair limit down when needed, and stops cleanly once quota reaches `0`.
- Successful website enrichment is reused while the normalized website URL stays the same.
- `--refresh-enrichment` forces website refetching for that run and stays persisted for resume.
- Campaign master exports are written to `exports/campaigns/<slug>/results.csv`, `results.json`, and `results.xlsx`.

## Quota And Storage

- Daily quota is fixed at `1000` new unique records per local day in the configured timezone.
- The default requested limit is `200`.
- SQLite state lives under `data/mapslead.sqlite3` unless you override `--data-dir` or `MAPSLEAD_DATA_DIR`.
- Per-run exports live under `exports/<run-id>/results.csv`, `results.json`, and `results.xlsx` unless you override `--export-dir` or `MAPSLEAD_EXPORT_DIR`.
- Campaign exports live under `exports/campaigns/<slug>/results.csv`, `results.json`, and `results.xlsx`.
- `data/` and `exports/` are local working directories and are not intended to be pushed to GitHub.

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
.venv/bin/mapslead campaign --help
.venv/bin/mapslead scrape --help
.venv/bin/mapslead --data-dir /tmp/mapslead-help-data quota
.venv/bin/mapslead resume --help
.venv/bin/mapslead export --help
```

## Live Smoke Procedure

This is the manual smoke path for a real networked run. Keep it capped at five requested results:

1. Ensure Docker is running and the provider image is present: `docker pull gosom/google-maps-scraper`
2. Start a small run: `.venv/bin/mapslead --data-dir ./smoke-data --export-dir ./smoke-exports scrape --business "dentists" --location "Ho Chi Minh City" --limit 5`
3. Confirm the CLI prints a run ID plus `results.csv`, `results.json`, and `results.xlsx` paths.
4. Inspect `./smoke-exports/<run-id>/results.csv`, `./smoke-exports/<run-id>/results.json`, and `./smoke-exports/<run-id>/results.xlsx`.
5. Re-run `.venv/bin/mapslead --data-dir ./smoke-data --export-dir ./smoke-exports quota` and confirm `Used today: 5`.

## Test Commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/mapslead
.venv/bin/python -m build
```
