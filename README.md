# LeadSutra Scraper

Discover local businesses, enrich their websites, and export structured leads.

[API guide](backend/README.md) · [CLI and configuration](lead_discovery/scraper/README.md)

## How it works

1. Accept a business query, location, result limit, and extraction mode.
2. Discover businesses through Google Places; use Maps browser fallback when applicable.
3. Crawl websites and extract the selected profile, contact, social, and technology data.
4. Score and filter leads when requested.
5. Return JSON through FastAPI and save the run files.

## Quick start — Windows CMD

- Use Python 3.11+; development and tests use Python 3.12.
- Run these commands from the project root.
- Create the environment and copy the template only if they do not already exist:

```cmd
py -3.12 -m venv venv
copy .env.example backend\.env
venv\Scripts\python.exe -m pip install -r backend\requirements.txt
venv\Scripts\python.exe -m playwright install chromium
```

- Add `GOOGLE_PLACES_API_KEY` to `backend/.env` for Places discovery.
- With no key, the scraper automatically uses the slower Maps browser path.
- Start the server without `--reload` on Windows so Playwright can start its subprocess:

```cmd
venv\Scripts\python.exe -m uvicorn backend.main:app
```

- Open [API docs](http://127.0.0.1:8000/docs).
- Submit this input to `POST /api/scraper/search`:

```json
{
  "query": "Dental clinic",
  "location": "Nashik",
  "limit": 5,
  "mode": "full",
  "qualification": "mixed"
}
```

## Output

- API response: run ID, result count, summary, warnings, leads, and local file paths.
- Result count is a maximum; fewer matching businesses may be available.
- Each API run creates:

```text
backend/scraper_outputs/<run_id>/
├── leads.json     # Exported lead records
├── summary.json   # Status, counts, qualification totals, and timings
└── scraper.log    # Module outcomes, warnings, and run details
```

- See the [response example](backend/README.md#response-example) for the exact lead structure.
- Partial enrichment keeps available business data; failed discovery returns HTTP 502.

## Speed and troubleshooting

- Website crawls and Maps detail lookups each run up to 3 at a time per run.
- Use `basic` for listing data only; `full` also crawls and scores.
- Qualification filters can inspect more candidates than the requested result count.
- Check `summary.timings_seconds` for discovery and website processing time.
- Routine failures print short warnings; unexpected programming errors retain tracebacks.
- Restart the backend after changing code or environment settings.

## Project layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI app, dependencies, local environment, API outputs |
| `lead_discovery/scraper/` | Discovery, crawling, extraction, scoring, and CLI |
| `lead_discovery/tests/` | Automated tests and HTML fixtures |
| `.env.example` | Shared environment template |
| `venv/` | Local Python environment; ignored by Git |

## Tests

```cmd
venv\Scripts\python.exe -m pip install pytest
cd lead_discovery
..\venv\Scripts\python.exe -m pytest tests -q
```

- Run tests from `lead_discovery` so the `scraper` imports resolve.
- Keep credentials in the ignored `backend/.env`; do not commit keys.
