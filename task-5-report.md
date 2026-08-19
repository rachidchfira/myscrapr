# Task 5 Report

- Base: `380d931`
- Commit: `236ef4ea823d5e2599a98e44adbc69a78c5d506d`
- Files:
  - `src/mapslead/exporter.py`
  - `tests/test_exporter.py`
  - `task-5-report.md`

## RED

- Added `tests/test_exporter.py` before the exporter implementation.
- Ran `.venv/bin/python -m pytest tests/test_exporter.py -q`.
- Result: collection failed with `ModuleNotFoundError: No module named 'mapslead.exporter'`.

## GREEN

- Implemented `Exporter.export_run(run_id: str) -> ExportPaths` in `src/mapslead/exporter.py`.
- Behavior implemented:
  - deterministic sort by normalized name, normalized address, then `business_id`
  - CSV field order fixed to the `RunSnapshot` export contract
  - CSV emails sorted and joined with `;`
  - JSON emails sorted as arrays, scalar missing values preserved as `null`
  - one CSV row and one JSON object per run-business association
  - temp-file write + `flush()` + `os.fsync()` for `results.csv.tmp` and `results.json.tmp`
  - paired replace with rollback so an existing valid export pair is restored on replace failure
  - temp and backup cleanup after success and failure

## Checks

- `.venv/bin/python -m pytest tests/test_exporter.py -q`
  - `3 passed`
- `.venv/bin/python -m ruff check src/mapslead/exporter.py tests/test_exporter.py`
  - `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
  - `Success: no issues found in 9 source files`

## Assumptions

- The export column order is the `RunSnapshot` field order plus `emails` rendered as a semicolon-separated CSV field.
- The exporter should wrap filesystem and serialization failures as `ExportError`, while repository lookup behavior remains owned by the repository port.
- Pair atomicity means previously valid `results.csv` and `results.json` must both remain unchanged if this export attempt fails during serialization or destination replacement.

## Review Fix Round 1

- Scope:
  - corrected the public export contract to the design-spec field set and order
  - removed internal `business_id` from CSV/JSON payloads while keeping it as the sort tiebreaker
  - renamed JSON/CSV review count output field to `reviews_count`
  - validated `run_id` as a single safe path component and rejected absolute paths, separators, and dot segments with typed `ExportError`
  - enforced resolved run-directory containment under the resolved export root before creating directories

### RED

- Updated `tests/test_exporter.py` first.
- Ran `.venv/bin/python -m pytest tests/test_exporter.py -q`.
- Result:
  - export golden test failed because the implementation still emitted internal fields and the old field order
  - unsafe `run_id` tests failed for `../escape`, `/absolute-run`, `nested/run`, `nested\\run`, `.`, and `..`

### GREEN

- Updated `src/mapslead/exporter.py` to emit only:
  - `place_id, name, category, address, phone, website, rating, reviews_count, google_maps_url, emails, facebook, instagram, linkedin, x, youtube, first_seen_at, last_seen_at, run_id`
- Added pre-write path validation with resolved containment enforcement.
- Preserved existing pair rollback and temp/backup cleanup behavior.

### Verification

- `.venv/bin/python -m pytest tests/test_exporter.py -q`
  - `9 passed`
- `.venv/bin/python -m ruff check src/mapslead/exporter.py tests/test_exporter.py`
  - `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
  - `Success: no issues found in 9 source files`

### Notes

- The earlier assumption about using the full `RunSnapshot` field order for exports is superseded by the review-mandated design-spec contract above.

## Fix Round 2

- Scope:
  - corrected the export contract to the authoritative 22-field design-spec order
  - restored `business_type`, `location_query`, `enrichment_status`, and `enrichment_error`
  - restored the exact field names `review_count`, `facebook_url`, `instagram_url`, `linkedin_url`, `x_url`, and `youtube_url`
  - continued excluding internal `business_id` from output while keeping it as the deterministic sort tiebreaker
  - preserved path hardening, pair rollback, and temp/backup cleanup

### RED

- Updated `tests/test_exporter.py` first to the authoritative 22-field order:
  - `place_id, name, category, address, phone, website, rating, review_count, google_maps_url, emails, facebook_url, instagram_url, linkedin_url, x_url, youtube_url, business_type, location_query, first_seen_at, last_seen_at, enrichment_status, enrichment_error, run_id`
- Ran `.venv/bin/python -m pytest tests/test_exporter.py -q`.
- Result:
  - golden export test failed because the implementation still emitted the reduced review-round field set

### GREEN

- Updated `src/mapslead/exporter.py` to emit exactly the authoritative 22-field order for both CSV and JSON.
- Confirmed:
  - CSV missing scalars serialize as empty fields
  - JSON missing scalars serialize as `null`
  - emails remain sorted in both formats
  - `enrichment_status` serializes as its string value
  - timestamps serialize as ISO 8601 strings

### Verification

- `.venv/bin/python -m pytest tests/test_exporter.py -q`
  - `9 passed`
- `.venv/bin/python -m ruff check src/mapslead/exporter.py tests/test_exporter.py`
  - `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
  - `Success: no issues found in 9 source files`

### Notes

- The reduced public field list from review round 1 is superseded by the authoritative design-spec field list above.
