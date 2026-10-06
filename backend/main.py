"""FastAPI endpoint that runs the existing LeadSutra scraper directly."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from lead_discovery.scraper.main import ScraperService

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def log_progress(message: str) -> None:
    level = logging.ERROR if message.startswith("[ERROR]") else logging.WARNING if message.startswith("[WARNING]") else logging.INFO
    logging.getLogger(__name__).log(level, message)

app = FastAPI(title="LeadSutra Scraper API", version="1.0.0")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    location: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=50, ge=1, le=100)
    mode: Literal["basic", "website", "profile", "contacts", "social", "technology", "full", "scoring", "custom"] = "full"
    qualification: Literal["qualified", "needs_review", "not_qualified", "mixed"] = "mixed"
    candidate_limit: int | None = Field(default=None, ge=1, le=100)
    fields: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
        "search": "POST /api/scraper/search",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scraper/search")
async def search(request: SearchRequest) -> dict:
    """Run discovery/enrichment using inputs supplied by the FastAPI caller."""
    if request.candidate_limit is not None and request.candidate_limit < request.limit:
        raise HTTPException(status_code=422, detail="candidate_limit must be >= limit")
    if request.mode == "custom" and not (request.fields or request.modules):
        raise HTTPException(status_code=422, detail="custom mode requires fields or modules")
    try:
        service = ScraperService(output_root=BASE_DIR / "scraper_outputs", progress=log_progress)
        run = await service.run(
            request.query, request.location, request.limit, request.mode,
            custom_fields=request.fields, custom_modules=request.modules,
            qualification=request.qualification, candidate_limit=request.candidate_limit,
        )
        leads_path = run.output_dir / "leads.json"
        summary_path = run.output_dir / "summary.json"
        leads = json.loads(leads_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("Scraper run failed")
        raise HTTPException(status_code=500, detail="Scraper run failed; inspect backend scraper logs") from exc
    warnings = getattr(run, "summary", {}).get("warnings", [])
    if summary.get("status") == "failed":
        raise HTTPException(status_code=502, detail={"run_id": run.run_id, "errors": warnings,
                                                   "message": "Business discovery failed"})
    return {
        "run_id": run.run_id,
        "search_query": request.query,
        "location": request.location,
        "total_results": len(leads),
        "summary": summary,
        "warnings": warnings,
        "leads": leads,
        "output_files": {
            "leads": str(leads_path.resolve()),
            "summary": str(summary_path.resolve()),
            "log": str((run.output_dir / "scraper.log").resolve()),
        },
    }
