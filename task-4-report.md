Base: `380d931ac87487798aaba30151daa731d87f9984`
Commit: `ffa20e1`

Files:
- `src/mapslead/enrichment.py`
- `tests/test_enrichment.py`
- `tests/fixtures/web/home.html`
- `tests/fixtures/web/contact.html`
- `task-4-report.md`

RED:
- Command: `./.venv/bin/python -m pytest tests/test_enrichment.py -q`
- Result: failed during collection with `ModuleNotFoundError: No module named 'mapslead.enrichment'`

GREEN:
- Command: `./.venv/bin/python -m pytest tests/test_enrichment.py -q`
- Result: `17 passed in 0.17s`

Checks:
- Command: `./.venv/bin/python -m ruff check src/mapslead/enrichment.py tests/test_enrichment.py`
- Result: `All checks passed!`
- Command: `./.venv/bin/python -m mypy src/mapslead`
- Result: `Success: no issues found in 9 source files`

Behavior:
- Added `UrlPolicy` to enforce `http`/`https`, reject URL credentials, and reject any hostname or redirect that resolves to a non-global IPv4 or IPv6 address.
- Added `WebsiteEnrichmentService` with same-registrable-domain discovery, deterministic Contact/About/Team prioritization, a four-page cap, robots gating, per-domain two-second spacing, and conversion of page-level failures into `EnrichmentResult(status=FAILED)` instead of escaping exceptions.
- Added offline extraction coverage for visible emails, `mailto:` links, social URL normalization, deduplication, cross-domain redirect rejection, robots warnings, and auth/download link filtering.

Assumptions:
- Robots fetch failures are surfaced through `EnrichmentResult.error` as warnings while still returning `status=COMPLETED`, because the frozen result model has no dedicated warnings field.
- A redirect that leaves the original registrable domain is treated as a failed enrichment attempt rather than a skipped page, matching the task’s redirect-safety requirement.
- This report artifact is intentionally left outside the required code commit so the implementation commit remains exactly `feat: enrich business websites safely`.

---

Review round 1

Base for round: `ffa20e1`

Findings addressed:
- Replaced the previous pre-resolution-only fetch approach with `SafeHttpTransport`, which resolves, selects, and connects to a specific public IP per request and rejects any connected peer IP that differs from that validated address, closing the DNS rebinding/TOCTOU gap at the actual connection boundary.
- Moved robots retrieval onto the same safe transport path so robots requests now inherit the same URL policy, redirect validation, and per-request pacing guarantees as page fetches.
- Tightened business-page discovery to Contact/About/Team candidates only and added a second robots check after page redirects before page content is trusted or crawled further.

RED:
- Command: `./.venv/bin/python -m pytest tests/test_enrichment.py -q`
- Result: failed during collection with `ImportError: cannot import name 'SafeHttpTransport' from 'mapslead.enrichment'`

GREEN:
- Command: `./.venv/bin/python -m pytest tests/test_enrichment.py -q`
- Result: `22 passed in 0.11s`

Checks:
- Command: `./.venv/bin/python -m ruff check src/mapslead/enrichment.py tests/test_enrichment.py`
- Result: `All checks passed!`
- Command: `./.venv/bin/python -m mypy src/mapslead`
- Result: `Success: no issues found in 9 source files`

Tradeoff:
- The concrete page fetch adapter no longer relies on Scrapling for network I/O. Preserving Scrapling would have left peer selection and redirect handling inside a transport that does not expose a safe way to pin the actual connected IP to the validated DNS result, so the adapter now uses a minimal direct HTTP/HTTPS requester that keeps URL validation, connection target, peer verification, redirects, and robots retrieval inside one auditable boundary.
