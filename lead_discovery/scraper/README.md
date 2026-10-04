# LeadSutra scraper configuration

Business discovery uses Places API (New) Text Search as its primary source. Set
`GOOGLE_PLACES_API_KEY` (or `GOOGLE_MAPS_API_KEY`) to a key restricted to the
Places API (New), with the API enabled and billing configured. The CLI itself is
unchanged:

```powershell
$env:GOOGLE_PLACES_API_KEY = "your-restricted-key"
..\venv\Scripts\python.exe -m scraper --query "Dental Clinics" --location "Nashik" --limit 20 --mode full
```

Places requests use `LEADSUTRA_PLACES_TIMEOUT_SECONDS` (default `15`),
`LEADSUTRA_PLACES_MAX_ATTEMPTS` (default `2`), and
`LEADSUTRA_PLACES_RETRY_DELAY_SECONDS` (default `0.15`). Text Search pages at
up to 20 results and currently returns at most 60 results for one query. Larger
limits are accepted but the source may return fewer records.

The existing Playwright Google Maps discovery is fallback only. If no Places
API key is configured, browser discovery runs automatically. For eligible
transient Places API failures, enable fallback with
`LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK=true`; zero-result fallback additionally
requires `LEADSUTRA_MAPS_FALLBACK_ON_ZERO_RESULTS=true`. Invalid credentials,
permission or billing failures, and invalid requests do not trigger fallback.
Check the applicable Google Maps Platform terms before enabling browser
fallback.

Each run writes only `leads.json` and `summary.json` in its run directory.
The summary includes field coverage, module statuses, warnings, discovery and
enrichment counts, and schema validation. Detailed extraction diagnostics stay
available in memory on `ScrapeRun.extraction_report`; raw page content is not
written to an extra report file or included in the final lead contract.

Use `--mode scoring` to score newly discovered businesses without crawling
websites, or `--module lead_scoring` with CUSTOM mode to request scoring alone.
FULL mode runs scoring after the selected enrichment modules. Scoring is local
and deterministic: it normalizes earned points over assessable weights and
reports evidence coverage separately. Category matching, score bands, and the
minimum evidence-coverage threshold are configurable through `ScoringConfig`.

To return up to five of the highest-scoring leads in the existing `Qualified`
band, use:

```powershell
..\venv\Scripts\python.exe -m scraper --query "Dental clinic" --location "Nashik" --limit 5 --mode full --qualification qualified
```

When a qualification filter is selected, the scraper scores a bounded candidate
pool before filtering. By default it examines at least 20 more candidates or
five times `--limit`, whichever is larger, with the pool capped at 60 when
`--limit` is 60 or less; use `--candidate-limit` to choose a different pool
size (it must be at least `--limit`). `--limit` controls how many
matching leads are returned, not how many businesses are initially searched.
If fewer candidates meet the requested band, fewer leads are returned and the
summary reports the candidate and match counts. Results are ranked by score
within that examined pool; the scraper cannot guarantee these are the top leads
across businesses it did not discover. `--qualification` accepts `qualified`,
`needs_review`, `not_qualified`, and `mixed` (the default, which preserves the
existing discovery-order behavior without filtering).

Google's current Places policy says Places content must not be pre-fetched,
cached, or stored beyond listed exceptions; Place IDs are exempt. The generated
lead export contains other Places fields, so verify the applicable project
terms before retaining these artifacts. If Places data is displayed outside a
Google Map, Google Maps attribution is required; third-party attributions must
also be preserved where returned. See Google's Places [policies and
attributions](https://developers.google.com/maps/documentation/places/web-service/policies).
