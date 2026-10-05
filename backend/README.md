# LeadSutra API

[Project setup](../README.md) · [Scraper configuration](../lead_discovery/scraper/README.md)

## Working

- FastAPI validates the request and awaits `ScraperService.run()`.
- The response arrives after discovery, selected enrichment, filtering, and export finish.
- Each request creates a separate run folder under `backend/scraper_outputs/`.
- The API loads `backend/.env`; existing process environment variables take precedence.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | API links and status |
| GET | `/health` | App liveness; does not test Google or browser availability |
| GET | `/docs` | Interactive request form |
| POST | `/api/scraper/search` | Run the scraper and return leads |

## Input

| Field | Default | Rules |
|---|---|---|
| `query` | Required | Nonblank string; maximum 300 characters |
| `location` | Required | Nonblank string; maximum 200 characters |
| `limit` | `50` | Integer, 1–100; maximum returned leads |
| `mode` | `full` | See extraction modes below |
| `qualification` | `mixed` | `mixed`, `qualified`, `needs_review`, `not_qualified` |
| `candidate_limit` | Automatic | Optional integer, 1–100 and at least `limit`; used for qualification filtering |
| `fields` | `[]` | Field names for `custom` mode |
| `modules` | `[]` | Module names for `custom` mode |

### Extraction modes

- `basic`: listing data only; no website crawl or scoring.
- `website`: website content extraction and website summary.
- `profile`: services, products, audience, about information, and hours.
- `contacts`: contact person, emails, and phone numbers.
- `social`: business social links.
- `technology`: technology detection from website evidence.
- `scoring`: score discovery data without crawling.
- `full`: all modules, including scoring.
- `custom`: selected fields/modules; requires at least one selection.
- All modes discover businesses. Profile, contacts, social, and technology request a website crawl.

### Request example

Send through `/docs` or POST this JSON to `/api/scraper/search`:

```json
{
  "query": "Dental clinic",
  "location": "Nashik",
  "limit": 1,
  "mode": "basic",
  "qualification": "mixed"
}
```

### Response example

Illustrative basic-mode response; IDs, business data, paths, and timings are sample values.
The `summary` below is an excerpt; actual responses include additional counters and qualification totals.

```json
{
  "run_id": "run_example",
  "search_query": "Dental clinic",
  "location": "Nashik",
  "total_results": 1,
  "summary": {
    "status": "success",
    "requested_limit": 1,
    "businesses_discovered": 1,
    "discovery_failures": 0,
    "total_leads": 1,
    "timings_seconds": {"discovery": 1.2, "websites": 0.0, "total": 1.3}
  },
  "warnings": [],
  "leads": [
    {
      "lead_id": "example_lead",
      "business": {
        "name": "Example Dental Clinic",
        "category": "Dentist",
        "sub_category": null,
        "description": null,
        "address": "Example Road, Nashik",
        "phone": null,
        "email": null,
        "website": "https://clinic.example/",
        "latitude": 19.9975,
        "longitude": 73.7898,
        "rating": 4.5
      },
      "profile": {
        "services": [], "products": [], "target_customers": [],
        "about_info": "", "operating_hours": {}
      },
      "website_analysis": {
        "status": "not_checked", "about": "", "services": [],
        "contact_page_url": null, "technology_stack": []
      },
      "contacts": {"contact_person": null, "emails": [], "phone_numbers": []},
      "social_links": {"facebook": null, "instagram": null, "linkedin": null},
      "lead_scoring": {
        "lead_score": null, "priority": "Unknown", "qualification_status": "Unknown"
      }
    }
  ],
  "output_files": {
    "leads": "D:/Projects/leadsutra_scapert/backend/scraper_outputs/run_example/leads.json",
    "summary": "D:/Projects/leadsutra_scapert/backend/scraper_outputs/run_example/summary.json",
    "log": "D:/Projects/leadsutra_scapert/backend/scraper_outputs/run_example/scraper.log"
  }
}
```

- All modes retain the same exported lead sections; missing data uses nulls or empty values, and unrequested scoring stays `Unknown`.
- `leads.json` contains the response's `leads` array, without the API wrapper.
- `output_files` contains server-local paths, not download URLs.
- Raw pages, extraction metadata, review counts, Twitter, and score breakdowns are not in the exported lead schema.

## Custom and filtered searches

Custom input — extract email addresses and technologies:

```json
{
  "query": "Dental clinic",
  "location": "Nashik",
  "limit": 5,
  "mode": "custom",
  "fields": ["emails", "technology_stack"]
}
```

- Whole modules: `business_discovery`, `website`, `profile`, `contacts`, `social`, `technology`, `lead_scoring`.
- Custom selection controls extraction work; it does not add fields to the export schema.
- Qualification filtering requires scoring: `full`, `scoring`, or custom with `lead_scoring`.
- Example: `limit: 10` with `qualification: "needs_review"` searches up to 50 candidates by default.
- Set `candidate_limit: 10` to inspect only 10 candidates; fewer than 10 may match.
- `mixed` keeps discovery order and uses `limit` as its candidate budget; other filters sort matches by score.

## Status and errors

| Result | Meaning |
|---|---|
| HTTP 200 | Completed run; inspect summary and warnings for partial enrichment |
| HTTP 422 | Validation error, unsupported selection, or a caught value/type error |
| HTTP 502 | Business discovery failed |
| HTTP 500 | Unexpected unhandled run failure |

- `summary.status`: `success`, `degraded`, or `failed`.
- `degraded`: enrichment errors or zero coverage of a required discovery field across all candidates.
- Partial enrichment means some requested information was unavailable; discovered leads are retained.
- Successful discovery with no results returns an empty array.
- Routine warnings are concise; unexpected programming errors keep console tracebacks.
- Timings measure pipeline processing before artifact writing and HTTP response delivery.

Example discovery failure:

```json
{
  "detail": {
    "run_id": "run_example",
    "errors": ["Business discovery failed: RuntimeError: browser unavailable"],
    "message": "Business discovery failed"
  }
}
```
