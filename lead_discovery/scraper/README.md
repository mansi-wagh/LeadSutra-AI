# Scraper and CLI

[Project setup](../../README.md) · [API inputs and JSON output](../../backend/README.md)

## Pipeline

1. Discover and deduplicate business listings.
2. Crawl candidate websites when the selected modules need them.
3. Extract requested data while preserving available results on local failures.
4. Apply deterministic scoring and optional qualification filtering.
5. Validate the exported schema; write leads, summary, and log files.

## CLI input

Run from `lead_discovery` in Windows CMD:

```cmd
cd lead_discovery
..\venv\Scripts\python.exe -m scraper --query "Dental clinic" --location "Nashik" --limit 5 --mode full
```

- The CLI reads process environment variables; it does **not** load `backend/.env`.
- To use Places from CMD, set `GOOGLE_PLACES_API_KEY` before running:

```cmd
set "GOOGLE_PLACES_API_KEY=your-key"
```

| Option | Default / purpose |
|---|---|
| `--query`, `--location` | Required, nonblank search inputs |
| `--limit` | `50`; positive maximum result count |
| `--mode` | `basic`; API defaults to `full` |
| `--qualification` | `mixed`; also accepts `qualified`, `needs_review`, `not_qualified` |
| `--candidate-limit` | Optional candidate budget for filtering; at least `--limit` |
| `--module`, `--field` | Repeatable custom selections |
| `--output-dir` | `scraper_outputs`, relative to the working directory |
| `--verbose` | Print per-business module statuses |
| `--headed` | Show browser windows |

- Modes: `basic`, `website`, `profile`, `contacts`, `social`, `technology`, `scoring`, `full`, `custom`.
- Custom example:

```cmd
..\venv\Scripts\python.exe -m scraper --query "Dental clinic" --location "Nashik" --limit 5 --mode custom --field emails --field technology_stack
```

- Filtered example: score up to 10 candidates and return at most 5 qualified leads:

```cmd
..\venv\Scripts\python.exe -m scraper --query "Dental clinic" --location "Nashik" --limit 5 --mode full --qualification qualified --candidate-limit 10
```

## Output

```text
scraper_outputs/<run_id>/
├── leads.json
├── summary.json
└── scraper.log
```

| File / object | Content |
|---|---|
| `leads.json` | Array of validated lead records; same structure as API `leads` |
| `summary.json` | Run status, counts, qualification totals, stage timings |
| `scraper.log` | Discovery source, module outcomes, warnings, file paths |
| `ScrapeRun.leads` | Internal models with extraction metadata |
| `ScrapeRun.summary` / `extraction_report` | Detailed in-memory diagnostics, coverage, and evidence |

Illustrative `scraper.log` excerpt:

```text
[DISCOVERY] businesses=10 status=success
[TIMING] {"discovery": 3.1, "websites": 8.4, "total": 11.7}
[QUALIFICATION] filter=qualified candidates=10 matched=3 returned=3
```

- Example: requesting 5 qualified leads can return 3 if only 3 candidates match.
- Timings are examples, not promised response times.
- See the [complete lead example](../../backend/README.md#response-example).
- Internal evidence/raw pages are not automatically saved as a separate report file.

## Discovery and fallback

- Primary source: Google Places Text Search.
- This client requests up to 20 places per page and caps one search at 60 results.
- No API key: automatically use Playwright Maps discovery.
- Retryable API failure with no partial records: browser fallback requires the fallback switch below.
- Later API request error: preserve collected records; credential/configuration errors still fail discovery.
- Zero API results: browser fallback requires both fallback switches.
- Rejected credentials, permission/configuration errors, and non-retryable API failures do not trigger browser fallback.

| Environment variable | Default |
|---|---|
| `GOOGLE_PLACES_API_KEY` | Empty; `GOOGLE_MAPS_API_KEY` is an alternate key name |
| `LEADSUTRA_PLACES_TIMEOUT_SECONDS` | `15` |
| `LEADSUTRA_PLACES_MAX_ATTEMPTS` | `2` |
| `LEADSUTRA_PLACES_RETRY_DELAY_SECONDS` | `0.15` |
| `LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK` | `false` |
| `LEADSUTRA_MAPS_FALLBACK_ON_ZERO_RESULTS` | `false` |

## Crawl limits

Configure through `WebsiteCrawlerConfig` in Python; these are not API request fields.

| Setting | Default |
|---|---|
| Concurrent website crawls per run | 3 |
| Page attempts per website | 8, including failed attempts |
| HTTP timeout | 15 seconds |
| Total crawl deadline per website | 40 seconds; partial data retained |
| Maximum response body | 2,000,000 bytes |
| Redirect budget | Up to 5 followed redirects per fetch |

- Maps detail lookups separately run up to 3 at a time.
- Static HTTP extraction runs first; short pages containing scripts may use browser rendering.
- Browser failures retain usable static content.
- Website fetching checks public IPs, pins DNS results, and permits HTTP(S) on ports 80/443.
- Cross-site page redirects are rejected; required trailing slashes are preserved.
- Website browser rendering blocks images, media, fonts, non-GET requests, service workers, and WebSockets.
- Browser resource requests are capped at 40 per browser instance; restricted sites may yield partial data.

## Scoring and latency

- Scoring is rule-based; it uses no LLM.
- Dimensions: relevance, website quality, reputation, digital presence, contact accessibility, and maturity.
- `ScoringConfig` controls thresholds and category keywords.
- `scoring` mode uses discovery data without crawling; evidence may be limited.
- `mixed` processes up to `limit` candidates without qualification filtering.
- Other filters use `candidate_limit`, or the default formula:
  `min(max(limit * 5, limit + 20), max(60, limit))`.
- Filtered results are ranked only within the examined pool.
- A Places key, fewer requested modules, and a smaller candidate budget can reduce work.
- Inspect `[TIMING]` to separate discovery and website latency.

## Source files

| File | Responsibility |
|---|---|
| `main.py` | CLI, pipeline, module selection, filtering, artifacts |
| `discovery.py` | Places client, Maps browser fallback, listing parsing |
| `enrichment.py` | Website crawling and extractors |
| `safe_http.py` | Public-address transport and bounded HTTP responses |
| `scoring_output.py` | Scoring rules and exported lead schema |

- Review [Google Places policies](https://developers.google.com/maps/documentation/places/web-service/policies) before retaining or displaying provider data.
