# Task 1 Report

## Scope and execution boundary

- Changed package scaffold and frozen domain contracts in `pyproject.toml`, `.gitignore`, `src/mapslead/__init__.py`, `src/mapslead/config.py`, `src/mapslead/models.py`, `src/mapslead/ports.py`, `src/mapslead/errors.py`, `src/mapslead/normalize.py`, and `tests/test_models.py`.
- Execution boundary analyzed: importable `mapslead` package contract for configuration, provider-facing models, cross-module protocols, typed errors, and normalization/identity helpers consumed by all later tasks.

## Concrete issue observed

- RED before implementation was an import-boundary failure: `tests/test_models.py` could not import `mapslead` because the package did not exist yet.
- A secondary environment gap appeared on the first RED attempt: `/opt/homebrew/bin/python3.12` had no global `pytest`, so I used `uvx --python /opt/homebrew/bin/python3.12 pytest tests/test_models.py -q` to capture the real failure before creating project metadata and the local virtual environment.

## Smallest safe fix

- Added the minimal `src/` package scaffold and `setuptools`-based `pyproject.toml` with the exact runtime/dev dependencies, pytest/Ruff/mypy configuration, and the frozen `mapslead = "mapslead.cli:app"` script contract required by later tasks.
- Implemented `Settings` with fixed quota/timezone semantics and `from_env()` limited to `MAPSLEAD_DATA_DIR` and `MAPSLEAD_EXPORT_DIR`.
- Implemented the frozen model surface for `RunStatus`, `EnrichmentStatus`, `ProviderCandidate`, `Identity`, `ProviderRequest`, `ProviderResult`, `EnrichmentResult`, `Acceptance`, `RunRecord`, `RunSnapshot`, `ExportPaths`, and `ProgressEvent`.
- Implemented the frozen protocol surface for `RepositoryPort`, `MapsProvider`, `CandidateSink`, `PageFetcher`, `WebsiteEnricher`, `ExporterPort`, `ProgressSink`, and `Clock`.
- Implemented typed application errors under `MapsLeadError`.
- Implemented `normalize_text`, `normalize_phone`, `registrable_domain`, and `build_identity` with the specified identity priority and rejection boundary.

## RED/GREEN evidence

### RED

```text
$ uvx --python /opt/homebrew/bin/python3.12 pytest tests/test_models.py -q
E   ModuleNotFoundError: No module named 'mapslead'
```

### GREEN

```text
$ .venv/bin/python -m pytest tests/test_models.py -q
8 passed in 0.13s
```

```text
$ .venv/bin/python -m ruff check src/mapslead tests/test_models.py
All checks passed!
```

```text
$ .venv/bin/python -m mypy src/mapslead
Success: no issues found in 6 source files
```

## Validation performed

- Primary success path: `build_identity()` prefers Place ID and falls back through normalized address, phone, then registrable domain exactly as required by `tests/test_models.py`.
- Representative failure path: candidates with blank names or without any complete identity pair raise `ValueError`, making the contract explicit to repository callers.
- Integration boundary: editable installation into the project-local `.venv` succeeded with `uv pip install --python .venv/bin/python -e '.[dev]'`, confirming the package metadata and import structure are usable by later tasks.

## Residual risk and environment-level follow-up

- `pyproject.toml` intentionally freezes the future CLI entry point at `mapslead.cli:app`, but `src/mapslead/cli.py` does not exist yet because it belongs to Task 7. Editable install succeeds today, but command execution cannot be validated until that module is implemented.
- `ProviderCandidate` currently models the Maps fields named in the design spec. If Task 3 needs additional provider-only raw columns, they should be added there only if the later tests prove the current frozen shape is insufficient.
