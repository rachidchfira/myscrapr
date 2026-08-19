# Task 6 Report

- Base: `de33608`
- Commit: `dc5da89`
- Files:
  - `src/mapslead/service.py`
  - `tests/test_service.py`
  - `task-6-report.md`

## RED

- Added `tests/test_service.py` before the service implementation.
- `python` was not available on `PATH` in this workspace, so the runnable equivalent was `.venv/bin/python`.
- Ran `.venv/bin/python -m pytest tests/test_service.py -q`.
- Result: collection failed with `ModuleNotFoundError: No module named 'mapslead.service'`.

## GREEN

- Implemented `MapsLeadService` in `src/mapslead/service.py` with:
  - pre-run requested-limit validation against the repository's current daily allowance
  - `scrape()` orchestration for run creation, provider acquisition, per-candidate persistence, enrichment checkpointing, final status selection, and export
  - `resume()` orchestration that replays durable provider rows first, recomputes remaining run capacity, limits any fresh acquisition by both run capacity and the current day's remaining quota, and keeps the same run ID
  - `RunOutcome`, `RequestedLimitError`, and `ResumeNotAllowedError`
  - a callable acquisition sink that stops accepting additional new records once the run or resume-attempt limit is reached while still allowing already-known duplicates through safely
  - provider-status precedence of `partial` on interruption, otherwise `blocked`/`failed`, otherwise `completed`
  - export-failure handling that keeps persisted records and run status durable while surfacing a readable `service_error`
  - concise progress events for acquisition, enrichment checkpoints, and final export paths without exposing candidate URLs or page bodies

## Checks

- `.venv/bin/python -m pytest tests/test_service.py -q`
  - `13 passed`
- `.venv/bin/python -m ruff check src/mapslead/service.py tests/test_service.py`
  - `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
  - `Success: no issues found in 11 source files`

## Assumptions

- Replay is allowed to ingest durable provider rows up to the run's persisted requested limit before any fresh acquisition attempt is considered.
- Export failure should not overwrite the run's provider-derived status or error; that operator-facing failure is returned separately as `RunOutcome.service_error`.
- Progress events need stable counters and final paths, but do not need to expose provider diagnostics or candidate-specific URL strings.

## Review Fix Round 1

- Scope:
  - caught transactional `QuotaExceededError` inside the acquisition sink so a precheck race does not leave the run `running` or crash the orchestration
  - once that race occurs, stop attempting further new uniques while still allowing already-known duplicates to replay safely
  - tightened resume eligibility so only `partial`, `blocked`, and `failed` runs are resumable; `running` and `completed` now fail fast with `ResumeNotAllowedError` before any provider call
  - honored `ProviderResult.status == "partial"` even when `interrupted` is `False`

### RED

- Added regression tests first in `tests/test_service.py` for:
  - quota-race handling during acquisition with terminal export and no lingering `running` status
  - rejecting `running` runs on resume before provider calls
  - honoring provider-reported `partial` status without the `interrupted` flag
- Ran `.venv/bin/python -m pytest tests/test_service.py -q`.
- Result:
  - `QuotaExceededError` escaped from the acquisition sink during the simulated race
  - provider-reported `partial` finalized the run as `completed`
  - `resume()` accepted a `running` run instead of raising `ResumeNotAllowedError`

### GREEN

- Updated `src/mapslead/service.py` to:
  - track a `quota_exhausted` sink state after a transactional race and suppress only later unknown candidates
  - keep processing known duplicates after quota exhaustion
  - reject resume for any status outside `{partial, blocked, failed}`
  - treat provider `partial` as terminal `RunStatus.PARTIAL` even without `interrupted=True`

### Verification

- `.venv/bin/python -m pytest tests/test_service.py -q`
  - `16 passed`
- `.venv/bin/python -m ruff check src/mapslead/service.py tests/test_service.py`
  - `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
  - `Success: no issues found in 11 source files`
