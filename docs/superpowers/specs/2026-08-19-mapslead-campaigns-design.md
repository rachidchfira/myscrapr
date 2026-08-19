# MapsLead Nationwide Campaigns Design

## Purpose

MapsLead needs to collect one nationwide dentist dataset through many city searches without mixing unrelated niches, counting the same business twice, repeatedly enriching an unchanged website, or forcing the operator to merge run exports manually.

The existing global business identity and daily quota remain authoritative. Campaigns add an explicit organizational and export layer; they do not create a second business database.

## Decisions

- Campaign membership is explicit. Runs never enter a campaign implicitly.
- A campaign has a stable slug and one locked normalized business type.
- A run can belong to at most one campaign. Existing legacy runs may remain unassigned.
- Canonical businesses remain global so deduplication works across campaigns and ordinary runs.
- Address alone is never a deduplication key because multiple legitimate businesses can share a building.
- Successful website enrichment is reused indefinitely while the normalized website URL is unchanged.
- A changed website URL invalidates the cached enrichment automatically.
- `--refresh-enrichment` forces website fetching for the current run and is persisted so resume behavior is deterministic.
- The 1,000-new-unique-record daily quota remains global across every campaign and non-campaign run.
- Campaign exports contain one row per campaign/business membership, not one row per discovery or run.
- Campaign deletion is out of scope. Future deletion must remove campaign associations only, never canonical businesses.

## Command Interface

```text
mapslead campaign create SLUG --business TEXT
mapslead campaign attach-run SLUG RUN_ID
mapslead campaign status SLUG
mapslead campaign export SLUG
mapslead scrape --campaign SLUG --location TEXT [--limit INTEGER=200] [--refresh-enrichment]
```

`scrape` keeps its existing `--business` option for non-campaign runs. When `--campaign` is supplied, the campaign's locked business type is used; passing `--business` as well is rejected to avoid conflicting inputs.

All campaign commands honor the existing application-level `--data-dir` and `--export-dir` options. Expected campaign errors are concise and do not display tracebacks.

The existing HCMC run `6f8d2ee1d37b44d7be6ce2413c0da825` will be attached to `vietnam-dentists` after the feature is installed. Attaching does not call Google Maps, enrich websites, or consume quota.

## Validation

Campaign slugs are lowercase ASCII letters, digits, and single hyphens, between 1 and 64 characters. They cannot begin or end with a hyphen. The slug is also the stable campaign ID and export directory component.

Business types and location queries use the existing whitespace and Unicode normalization rules. A run may be attached only when its normalized business type equals the campaign's normalized business type. Attaching a missing run, a run already assigned to another campaign, or a mismatched run fails atomically.

Creating an existing slug fails rather than silently changing its business type. Campaign business type is immutable.

## Persistence

SQLite adds the following tables:

```sql
campaigns(
    slug TEXT PRIMARY KEY,
    business_type TEXT NOT NULL,
    normalized_business_type TEXT NOT NULL,
    created_at TEXT NOT NULL
)

campaign_runs(
    campaign_slug TEXT NOT NULL REFERENCES campaigns(slug),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
    attached_at TEXT NOT NULL,
    PRIMARY KEY(campaign_slug, run_id)
)

campaign_businesses(
    campaign_slug TEXT NOT NULL REFERENCES campaigns(slug),
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    first_discovered_at TEXT NOT NULL,
    last_discovered_at TEXT NOT NULL,
    PRIMARY KEY(campaign_slug, business_id)
)

business_enrichment_cache(
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
    normalized_website TEXT NOT NULL,
    result_json TEXT NOT NULL,
    completed_at TEXT NOT NULL
)
```

The existing `runs` table gains a non-null `refresh_enrichment` field through an idempotent schema migration. `campaign_runs` is the single source of truth for run membership, avoiding a duplicated campaign reference. Existing databases and runs remain valid.

Creating a campaign run stores the campaign association in the same transaction as the run. Accepting a candidate into a campaign run upserts `campaign_businesses` in the same transaction as the run/business association. Attaching an existing run inserts its `campaign_runs` row and all memberships in one transaction. It also seeds the global enrichment cache from successful attached-run snapshots whose normalized website still matches the canonical business, so attaching the existing HCMC run does not cause those websites to be fetched again.

## Deduplication And Discovery History

The existing repository identity rules remain unchanged:

1. Google Place ID.
2. Normalized name plus normalized address.
3. Normalized name plus normalized phone.
4. Normalized name plus registrable website domain.

Repeated results may still be returned by the external Maps provider, but they resolve to the same canonical `business_id`, do not consume quota again, and create only one campaign membership.

Every campaign business derives discovery history from its associated run snapshots. `discovered_in` is the sorted, distinct set of location queries that found the business. A business discovered in Hanoi and HCMC remains one master row with both locations recorded.

## Enrichment Cache

Before fetching a pending website, the service checks `business_enrichment_cache`:

- If a successful cache entry has the same normalized website URL and the run does not force refresh, copy the cached result into the run snapshot without a network request.
- If the website differs, no successful cache exists, or refresh is forced, fetch normally.
- A successful enrichment upserts the cache.
- Failed, skipped, robots-disallowed, or unsafe-URL results are checkpointed on the run but do not replace a successful cache entry.
- Businesses without a website remain skipped and create no cache entry.

Cache reuse emits a concise progress event indicating reuse without exposing URLs or page content.

## Campaign Master Export

Campaign exports are written atomically to:

```text
<export-dir>/campaigns/<slug>/results.csv
<export-dir>/campaigns/<slug>/results.json
```

Rows sort by normalized name, normalized address, then canonical business ID. Each campaign/business membership appears exactly once.

The field order is:

```text
place_id
name
category
address
phone
website
rating
review_count
google_maps_url
emails
facebook_url
instagram_url
linkedin_url
x_url
youtube_url
business_type
first_seen_at
last_seen_at
enrichment_status
enrichment_error
campaign_id
discovered_in
```

Canonical business fields use the latest merged canonical record. Enrichment fields use the matching successful cache when available; otherwise they use the most recent campaign snapshot. CSV represents `emails` and `discovered_in` as sorted semicolon-separated values. JSON represents them as sorted arrays. Missing JSON scalar values are `null`; missing CSV scalar values are empty.

Serialization, fsync, paired CSV/JSON replacement, rollback, temporary-file cleanup, and path containment follow the existing run exporter guarantees.

## Campaign Status

`campaign status` reports:

- campaign slug and business type;
- number of attached runs;
- number of unique campaign businesses;
- sorted discovery locations;
- enrichment counts for completed, failed, skipped, and pending businesses;
- current global daily quota used and remaining;
- master export paths when they exist.

## Error And Resume Behavior

Provider blocked, failed, and partial states retain the existing run lifecycle. Accepted records and campaign memberships remain durable and exportable. Resume uses the persisted campaign and refresh settings; it cannot move a run to another campaign.

Campaign export failure never removes memberships or run data. Attaching a run and campaign membership upserts are idempotent. Repeating `attach-run` for the same campaign/run succeeds without duplicating data; attempting to attach it to a different campaign fails.

## Testing

All automated tests remain offline. Coverage includes:

- slug and immutable business-type validation;
- campaign isolation between dentists and another niche;
- atomic attachment of the existing HCMC run;
- rejection of mismatched and already-assigned runs;
- global deduplication and unchanged quota accounting across city runs;
- one campaign membership for repeated provider results;
- successful cache reuse without calling the fetcher;
- cache invalidation on website change and forced refresh behavior;
- failed enrichment not replacing a successful cache;
- deterministic one-row-per-business master CSV/JSON with sorted discovery locations;
- campaign export rollback and path containment;
- status counts and CLI error/help behavior;
- database migration from the current schema;
- full offline end-to-end flow: create campaign, attach HCMC run, scrape a second city, reuse enrichment, and export the master dataset.

No automated test contacts Google Maps, Docker, or business websites.
