# Task 7 Report

## Workflow Boundary

Changed the `mapslead` CLI entrypoint for the operator workflow spanning:

- `mapslead scrape`
- `mapslead quota`
- `mapslead resume`
- `mapslead export --run-id`

Owned files:

- [src/mapslead/cli.py](/Users/rachd/Documents/Codex/2026-08-19/is/mapslead/.worktrees/mapslead-mvp/src/mapslead/cli.py)
- [tests/test_cli.py](/Users/rachd/Documents/Codex/2026-08-19/is/mapslead/.worktrees/mapslead-mvp/tests/test_cli.py)
- [README.md](/Users/rachd/Documents/Codex/2026-08-19/is/mapslead/.worktrees/mapslead-mvp/README.md)
- [.gitignore](/Users/rachd/Documents/Codex/2026-08-19/is/mapslead/.worktrees/mapslead-mvp/.gitignore)

## Primary Friction Source

The branch had no CLI module at all, so the intended operator workflow was missing end-to-end. Evidence:

- RED step: `tests/test_cli.py` initially failed on `ModuleNotFoundError: No module named 'mapslead.cli'`.
- There was no command surface for quota inspection, resume, export regeneration, or path override precedence.
- There was no prerequisite gate to fail fast before `Repository.create_run`.
- `export` could otherwise delegate straight to the exporter, which would silently regenerate an empty export pair for an unknown run ID because exporter validation is repository-agnostic.

## Smallest Safe Change

Implemented one new CLI module instead of widening service or repository APIs:

- Added a Typer app with `scrape`, `quota`, `resume`, and `export`.
- Kept dependency injection at `build_service(settings)` so tests can replace the runtime cleanly.
- Added a focused `OperatorPrerequisiteChecker` that verifies:
  - `docker version`
  - `docker image inspect gosom/google-maps-scraper`
  - `scrapling.fetchers.Fetcher` import
  - Playwright Chromium executable path exists
- Routed expected operator failures to concise messages and stable exit codes instead of tracebacks.
- Rendered concise progress lines and final export paths.
- Added an offline end-to-end CLI test using the real repository, service, exporter, fake provider, and fixture fetcher.
- Added an `export` run-existence guard in the CLI to avoid silent empty exports for unknown run IDs.

Tradeoffs:

- `resume` now performs the same prerequisite gate as `scrape`. This is a small extension beyond the “before run creation” requirement, but it improves operator reliability because `resume` can call the provider again.
- Verified commands in the README use `.venv/bin/...` because `python` was not available on PATH in this environment.

## Validations Performed

RED:

- `.venv/bin/python -m pytest tests/test_cli.py -q`
  - Confirmed initial failure on missing `mapslead.cli`.

Automated:

- `.venv/bin/python -m pytest -q`
  - Result: `104 passed`
- `.venv/bin/python -m ruff check .`
  - Result: success
- `.venv/bin/python -m mypy src/mapslead`
  - Result: success
- `.venv/bin/python -m build`
  - Result: built `mapslead-0.1.0.tar.gz` and `mapslead-0.1.0-py3-none-any.whl`

CLI contract and help:

- `.venv/bin/mapslead --help`
- `.venv/bin/mapslead scrape --help`
- `.venv/bin/mapslead --data-dir /tmp/mapslead-help-data quota`
  - Result: `Used today: 0` and `Remaining: 1000`
- `.venv/bin/mapslead resume --help`
- `.venv/bin/mapslead export --help`

Coverage specifics:

- normal path: offline scrape with real repository/service/exporter and stable re-export
- failure path: prerequisite failures and quota/requested-limit errors without tracebacks
- integration edge: CLI path overrides beat env vars without changing timezone/quota semantics

## Remaining Environment-Level Checks

- A real live smoke run with Docker, the provider image, Google Maps access, and Chromium installed was documented but not executed in automation.
- Resume against an actual interrupted Docker-backed run still needs manual validation in the target operator machine.
- Shell completion generation was not requested and was not added.

## Residual Risk And Follow-Up

Residual risk:

- The CLI prerequisite checker intentionally maps several Playwright/bootstrap failures to the single Chromium remediation message. That is the safest operator-facing default here, but it can mask the exact low-level Playwright cause.

Prioritized follow-up:

1. Run the documented five-record live smoke on the target workstation.
2. Capture one real blocked/partial provider run and verify the `resume` operator path with Docker present.
3. If operators need finer diagnostics later, split Playwright import/bootstrap failures from missing-browser failures without changing the current command contract.

## Review Fix Round 1

Date: August 19, 2026

### Issue Addressed

Important 1:

- The CLI was blocking `scrape` and `resume` on Scrapling import and Playwright Chromium checks even though the current safe enrichment runtime uses direct HTTP and does not require those operator prerequisites.

Important 2:

- `resume` was probing Docker before confirming that the run existed and was in a resumable state, which could hide the real operator error and do unnecessary prerequisite work.

### Smallest Safe Correction

- Reduced `OperatorPrerequisiteChecker` to the actual acquisition prerequisites only:
  - `docker version`
  - `docker image inspect gosom/google-maps-scraper`
- Added CLI-side resumability preflight using repository state before any Docker/image checks.
- Kept the existing service-level resume validation in place so the CLI preflight is additive safety, not the only enforcement layer.
- Updated README wording and smoke instructions to remove the false Chromium/browser requirement.

### RED To GREEN

Added and ran failing CLI tests first for:

- missing run on `resume` must error before any prerequisite checks
- completed run on `resume` must error before any prerequisite checks
- running run on `resume` must error before any prerequisite checks
- resumable run still performs prerequisite checks before calling `service.resume`
- prerequisite checker no longer depends on Scrapling or Chromium

Initial RED result:

- `tests/test_cli.py` failed because `resume` checked prerequisites too early and the tests still reflected the removed browser gates.

GREEN result:

- `.venv/bin/python -m pytest tests/test_cli.py -q`
  - Result: `20 passed`

### Verification

- `.venv/bin/python -m pytest -q`
- `.venv/bin/python -m ruff check .`
- `.venv/bin/python -m mypy src/mapslead`
- `.venv/bin/python -m build`

### Compatibility Note

- I did not edit `pyproject.toml` because it is outside the owned file set for this task round.
- Follow-up for parent: re-evaluate whether `scrapling` and any Playwright/browser-related runtime dependency expectations should remain in project metadata now that operator-time enrichment uses the direct HTTP transport path.
