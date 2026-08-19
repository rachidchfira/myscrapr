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

## Fix round 1: bare-domain website normalization

### Scope and execution boundary

- Changed `src/mapslead/normalize.py` and `tests/test_models.py`.
- Execution boundary analyzed: website-to-hostname normalization inside `build_identity()` for `name_domain` fallback key generation.

### Concrete issue observed

- Bare-domain website values like `example.com` and `www.example.com` were rejected.
- Root cause: `_normalized_hostname()` trusted `urlsplit(trimmed).hostname` only. Without a scheme, `urlsplit()` places the domain in the path component, leaving `hostname` as `None`, so `registrable_domain()` returned `None` and `build_identity()` raised `ValueError("candidate identity is incomplete")`.

### Smallest safe fix

- Retried parsing with an implied `https://` scheme only when the first `urlsplit()` call produced no hostname.
- This preserves existing behavior for already-qualified URLs and fixes only the bare-domain parsing gap required by the review.

### RED/GREEN evidence

```text
$ .venv/bin/python -m pytest tests/test_models.py -q -k bare_domain_website
FF                                                                       [100%]
=================================== FAILURES ===================================
________ test_bare_domain_website_is_accepted_for_identity[example.com] ________
E           ValueError: candidate identity is incomplete

______ test_bare_domain_website_is_accepted_for_identity[www.example.com] ______
E           ValueError: candidate identity is incomplete

=========================== short test summary info ============================
FAILED tests/test_models.py::test_bare_domain_website_is_accepted_for_identity[example.com]
FAILED tests/test_models.py::test_bare_domain_website_is_accepted_for_identity[www.example.com]
2 failed, 8 deselected in 0.12s
```

```text
$ .venv/bin/python -m pytest tests/test_models.py -q -k bare_domain_website
..                                                                       [100%]
2 passed, 8 deselected in 0.14s
```

```text
$ .venv/bin/python -m pytest tests/test_models.py -q
..........                                                               [100%]
10 passed in 0.15s
```

```text
$ .venv/bin/python -m ruff check src/mapslead tests/test_models.py
All checks passed!
```

```text
$ .venv/bin/python -m mypy src/mapslead
Success: no issues found in 6 source files
```

### Exact commands run

```text
.venv/bin/python -m pytest tests/test_models.py -q -k bare_domain_website
.venv/bin/python -m pytest tests/test_models.py -q
.venv/bin/python -m ruff check src/mapslead tests/test_models.py
.venv/bin/python -m mypy src/mapslead
```

### Validation performed

- Primary success path: `build_identity()` now accepts both `example.com` and `www.example.com` and produces `name_domain:example dental|example.com`.
- Representative failure path retained: candidates without any complete identity still raise `ValueError`, so the rejection boundary remains explicit to repository callers.
- Integration boundary: the change is import-local to `normalize.py` and does not alter model, protocol, or package metadata contracts used by later tasks.
