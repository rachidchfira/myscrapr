Status: DONE_WITH_CONCERNS

Base: `ad2918580423db9e347c23a4820b08c1d85c6a0c`
Head: `c4beadbee4704cf04b350727c43d76a767199bb0`

Changed Files:
- `src/mapslead/models.py`
- `src/mapslead/errors.py`
- `src/mapslead/normalize.py`
- `src/mapslead/ports.py`
- `src/mapslead/repository.py`
- `tests/test_campaign_repository.py`
- `.superpowers/sdd/2026-08-19-mapslead-campaigns/task-1-report.md`

RED:
- `.venv/bin/python -m pytest tests/test_campaign_repository.py -q`
- Result: collection error importing `CampaignBusinessTypeError` from `mapslead.errors`

GREEN:
- `.venv/bin/python -m pytest tests/test_campaign_repository.py -q`
- Result: `18 passed in 0.18s`

Checks:
- `.venv/bin/python -m pytest tests/test_campaign_repository.py tests/test_repository.py tests/test_models.py -q`
- Result: `41 passed, 1 failed`
- Remaining failure: `tests/test_repository.py::test_initialize_creates_schema_version_one` still expects schema version `1`, but Task 1 requires schema version `2`
- `.venv/bin/python -m ruff check src/mapslead/models.py src/mapslead/errors.py src/mapslead/normalize.py src/mapslead/ports.py src/mapslead/repository.py tests/test_campaign_repository.py`
- Result: `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
- Result: `Success: no issues found in 12 source files`

Migrations Tested:
- Added a real schema-version-1 SQLite fixture and verified `initialize()` migrates it to schema version `2`
- Verified the migrated run remains `completed`, keeps its snapshot payload, preserves quota usage, and defaults `runs.refresh_enrichment` to `0`

Concerns:
- The broader repository suite still contains one stale assertion for schema version `1`; Task 1 code follows the required schema version `2` contract.
- This report was created after the required feature commit so the workspace now includes this uncommitted report file.

Follow-up Correction:
- Updated `tests/test_repository.py` to assert fresh initialization creates schema version `2`, the four new campaign/cache tables, and `runs.refresh_enrichment INTEGER`
- This resolves the stale version-1 expectation without changing production behavior

Follow-up Checks:
- `.venv/bin/python -m pytest tests/test_campaign_repository.py tests/test_repository.py tests/test_models.py -q`
- Result: `42 passed in 0.53s`
- `.venv/bin/python -m ruff check src/mapslead/models.py src/mapslead/errors.py src/mapslead/normalize.py src/mapslead/ports.py src/mapslead/repository.py tests/test_campaign_repository.py tests/test_repository.py`
- Result: `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
- Result: `Success: no issues found in 12 source files`

Resolved Concerns:
- The stale schema-version assertion in `tests/test_repository.py` is corrected to the approved version-2 contract.
- This report is now intended to be committed with the follow-up test correction.

Review Fix Round 1:
- Finding addressed: campaign membership timestamps for reused global businesses incorrectly inherited the global business first-seen timestamp on first campaign discovery.
- Scope: `src/mapslead/repository.py`, `tests/test_campaign_repository.py`

Review Fix Round 1 RED:
- `.venv/bin/python -m pytest tests/test_campaign_repository.py -q -k membership_timestamps_start_at_first_campaign_discovery`
- Result: `1 failed, 18 deselected`
- Failure: `CampaignSnapshot.first_seen_at` was `2026-08-18T10:00:00+00:00` from the earlier non-campaign sighting instead of the first campaign discovery time `2026-08-19T10:00:00+00:00`

Review Fix Round 1 GREEN:
- `.venv/bin/python -m pytest tests/test_campaign_repository.py -q -k membership_timestamps_start_at_first_campaign_discovery`
- Result: `1 passed, 18 deselected in 0.11s`

Review Fix Round 1 Checks:
- `.venv/bin/python -m pytest tests/test_campaign_repository.py tests/test_repository.py tests/test_models.py -q`
- Result: `43 passed in 0.53s`
- `.venv/bin/python -m ruff check src/mapslead/repository.py tests/test_campaign_repository.py`
- Result: `All checks passed!`
- `.venv/bin/python -m mypy src/mapslead`
- Result: `Success: no issues found in 12 source files`

Review Fix Round 1 Notes:
- Candidate acceptance for campaign runs now seeds a new campaign membership from the campaign discovery time `now`; later sightings continue to advance `last_discovered_at`.
- Existing `attach_run` behavior is unchanged and still seeds campaign membership timestamps from attached run snapshots.
