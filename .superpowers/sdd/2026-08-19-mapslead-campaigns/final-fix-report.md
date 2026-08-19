Final audit fix report
======================

Scope
-----
- Fixed same-run duplicate snapshot updates in `src/mapslead/repository.py`.
- Fixed `CampaignSnapshot.business_type` to use the campaign's immutable stored display value.
- Added repository regressions for URL-change reset behavior, normalization-equivalent URL preservation, and campaign business type display.
- Updated one older repository test to reflect the new same-run snapshot behavior.

RED
---
- `.venv/bin/python -m pytest tests/test_repository.py -q -k 'same_run_website_change or normalization_equivalent_website'`
  - 2 failures:
    - snapshot kept old website instead of updating to the new same-run provider website
    - normalization-equivalent duplicate sighting did not refresh the stored raw website value
- `.venv/bin/python -m pytest tests/test_campaign_repository.py -q -k 'campaign_business_type_display'`
  - 1 failure:
    - campaign snapshot exposed the run formatting (`dentists`) instead of the campaign display value (`  Dentists  `)

GREEN
-----
- `.venv/bin/python -m pytest tests/test_repository.py -q -k 'same_run_website_change or normalization_equivalent_website'`
  - 2 passed.
- `.venv/bin/python -m pytest tests/test_campaign_repository.py -q -k 'campaign_business_type_display'`
  - 1 passed.
- `.venv/bin/python -m pytest -q`
  - 168 passed.
- `.venv/bin/python -m ruff check src/mapslead/repository.py tests/test_repository.py tests/test_campaign_repository.py`
  - clean.
- `.venv/bin/python -m mypy src/mapslead`
  - clean.

Behavior change summary
-----------------------
- When the same business is accepted again in the same run, the run snapshot now refreshes its provider-facing fields and `last_seen_at`.
- If the normalized website changes, that same-run snapshot resets enrichment data to pending so the old successful cache is not reused.
- If the website string only changes in normalization-equivalent ways, completed enrichment is preserved while the raw website field updates to the latest provider value.
- Campaign snapshots now present the campaign's stored business type display exactly as created.
