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
