import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import main as api


def test_search_passes_fastapi_inputs_and_returns_approved_lead_schema(tmp_path, monkeypatch):
    lead = {
        "lead_id": "lead_001",
        "business": {"name": "ABC Dental Clinic", "category": "Dentist", "sub_category": None,
                     "description": None, "address": None, "phone": None, "email": None,
                     "website": None, "latitude": None, "longitude": None, "rating": None},
        "profile": {"services": [], "products": [], "target_customers": [], "about_info": "", "operating_hours": {}},
        "website_analysis": {"status": "active", "about": None, "services": [],
                             "contact_page_url": None, "technology_stack": []},
        "contacts": {"contact_person": None, "emails": [], "phone_numbers": []},
        "social_links": {"facebook": None, "instagram": None, "linkedin": None},
        "lead_scoring": {"lead_score": 0, "priority": "Unknown", "qualification_status": "Unknown"},
    }
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    (run_dir / "leads.json").write_text(json.dumps([lead]), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps({"total_leads": 1}), encoding="utf-8")
    (run_dir / "scraper.log").write_text("run", encoding="utf-8")
    captured = {}

    class FakeScraper:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        async def run(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(run_id="run_test", output_dir=run_dir)

    monkeypatch.setattr(api, "ScraperService", FakeScraper)
    response = TestClient(api.app).post("/api/scraper/search", json={
        "query": "Dental clinic", "location": "Nashik", "limit": 5,
        "mode": "full", "qualification": "qualified",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["leads"] == [lead]
    assert body["total_results"] == 1
    assert captured["args"] == ("Dental clinic", "Nashik", 5, "full")
    assert captured["kwargs"]["qualification"] == "qualified"
    assert "evidence_used" not in json.dumps(body["leads"])


def test_custom_mode_requires_fields_or_modules():
    response = TestClient(api.app).post("/api/scraper/search", json={
        "query": "Dentist", "location": "Nashik", "mode": "custom",
    })
    assert response.status_code == 422


def test_root_shows_available_api_paths():
    response = TestClient(api.app).get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"
    assert response.json()["search"] == "POST /api/scraper/search"


def test_failed_discovery_returns_502_with_run_id_and_error(tmp_path, monkeypatch):
    (tmp_path / 'leads.json').write_text('[]')
    (tmp_path / 'summary.json').write_text('{"status":"failed"}')
    class FakeScraper:
        def __init__(self, **kwargs): pass
        async def run(self, *args, **kwargs):
            return SimpleNamespace(run_id='failed-run', output_dir=tmp_path,
                                   summary={'warnings': ['Business discovery failed: provider unavailable']})
    monkeypatch.setattr(api, 'ScraperService', FakeScraper)
    response = TestClient(api.app).post('/api/scraper/search', json={'query': 'Dentist', 'location': 'Nashik'})
    assert response.status_code == 502
    assert response.json()['detail']['run_id'] == 'failed-run'
    assert 'provider unavailable' in response.json()['detail']['errors'][0]
