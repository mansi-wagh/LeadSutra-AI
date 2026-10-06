import asyncio
import json

import pytest

from scraper.discovery import BusinessRecord
from scraper.main import ScraperOrchestrator
from scraper.enrichment import CrawlError, WebsiteCrawlResult, WebsiteLink, WebsitePage, WebsiteStatus


def make_business(lead_id="lead-a"):
    return BusinessRecord.from_extracted({
        "lead_id": lead_id, "business_name": "Acme Studio",
        "website": "https://acme.example", "source_ref": "https://maps.example/acme",
    })


def make_site(lead_id="lead-a", pages=True):
    content = []
    if pages:
        content = [WebsitePage(
            page_url="https://acme.example/", page_type="home",
            main_text="Contact sales@acme.example",
            meta_description="A studio serving small businesses.",
            outgoing_links=[
                WebsiteLink(url="mailto:sales@acme.example"),
                WebsiteLink(url="https://instagram.com/acme-studio", text="Instagram"),
            ],
            technology_signals=["meta-generator:WordPress"],
        )]
    return WebsiteCrawlResult(
        lead_id=lead_id, website_status=WebsiteStatus.ACTIVE if pages else WebsiteStatus.NOT_FOUND,
        website_url="https://acme.example", website_content=content,
        pages_visited=[page.page_url for page in content],
        field_sources={"website_content": [page.page_url for page in content]},
    )


class FakeDiscovery:
    def __init__(self, records): self.records, self.calls = records, []
    async def search(self, query, location, limit):
        self.calls.append((query, location, limit))
        return self.records[:limit]


class FakeCrawler:
    def __init__(self, config, browser_config, result):
        self.result, self.calls = result, 0
    async def crawl(self, business):
        self.calls += 1
        return self.result


def setup(tmp_path, monkeypatch, *, no_pages=False, fail_contacts=False, records=None):
    instances = []
    discovery = FakeDiscovery(records or [make_business()])
    def factory(config, browser_config):
        item = FakeCrawler(config, browser_config, make_site(pages=not no_pages))
        instances.append(item)
        return item
    if fail_contacts:
        monkeypatch.setattr(
            "scraper.main.extract_contacts",
            lambda *args: (_ for _ in ()).throw(RuntimeError("mock contact failure")),
        )
    app = ScraperOrchestrator(
        discovery=discovery, website_crawler_factory=factory, output_root=tmp_path,
    )
    return app, instances, discovery


def call(app, mode, **kwargs):
    return asyncio.run(app.run("dentists", "Nashik", 20, mode, **kwargs))


@pytest.mark.parametrize('selection', [{'custom_fields': ['technology_stack']}, {'custom_modules': ['technology']}])
def test_custom_technology_survives_export(tmp_path, monkeypatch, selection):
    app, _, _ = setup(tmp_path, monkeypatch)
    run = call(app, 'custom', **selection)
    payload = json.loads((run.output_dir / 'leads.json').read_text())
    assert payload[0]['website_analysis']['technology_stack'] == ['WordPress']


def test_discovery_failure_is_failed_and_printed(tmp_path, caplog):
    class BrokenDiscovery:
        async def search(self, *args): raise RuntimeError('provider unavailable')
    messages = []
    run = call(ScraperOrchestrator(discovery=BrokenDiscovery(), output_root=tmp_path, progress=messages.append), 'basic')
    assert run.summary['status'] == 'failed'
    assert any('provider unavailable' in message for message in messages)
    assert 'provider unavailable' not in caplog.text  # Progress callback owns console delivery.


def test_failure_without_progress_callback_is_logged_once(tmp_path, caplog):
    class BrokenDiscovery:
        async def search(self, *args): raise RuntimeError('provider unavailable')
    call(ScraperOrchestrator(discovery=BrokenDiscovery(), output_root=tmp_path), 'basic')
    errors = [record for record in caplog.records if 'provider unavailable' in record.getMessage()]
    assert len(errors) == 1
    assert not errors[0].exc_info


def test_logging_progress_callback_does_not_duplicate_errors(tmp_path, caplog):
    import logging
    class BrokenDiscovery:
        async def search(self, *args): raise RuntimeError('provider unavailable')
    call(ScraperOrchestrator(discovery=BrokenDiscovery(), output_root=tmp_path,
                            progress=logging.getLogger('test.progress').warning), 'basic')
    assert sum('provider unavailable' in record.getMessage() for record in caplog.records) == 1


def test_zero_results_is_success(tmp_path):
    run = call(ScraperOrchestrator(discovery=FakeDiscovery([]), output_root=tmp_path), 'basic')
    assert run.summary['status'] == 'success'


def test_crawls_overlap_with_limit_and_preserve_order_and_failures(tmp_path):
    active = peak = 0
    class Crawler:
        async def crawl(self, record):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                if record.lead_id == 'lead-2':
                    raise RuntimeError('one site failed')
                return make_site(record.lead_id)
            finally:
                active -= 1
    records = [make_business(f'lead-{i}') for i in range(5)]
    app = ScraperOrchestrator(discovery=FakeDiscovery(records), output_root=tmp_path,
                             website_crawler_factory=lambda *_: Crawler())
    run = call(app, 'website')
    assert peak == 3
    assert [lead.business.lead_id for lead in run.leads] == [record.lead_id for record in records]
    assert run.leads[2].extraction_metadata.module_status['website'] == 'failed'
    assert run.summary['timings_seconds']['websites'] > 0


def test_basic_never_crawls_and_writes_consistent_json(tmp_path, monkeypatch):
    app, crawlers, _ = setup(tmp_path, monkeypatch)
    run = call(app, "basic")
    lead = run.leads[0].model_dump(mode="json")
    assert not crawlers
    assert set(lead) == {"business", "profile", "website_analysis", "contacts", "social_links", "lead_scoring", "extraction_metadata"}
    assert lead["website_analysis"] is None
    assert lead["lead_scoring"]["lead_score"] is None
    assert lead["lead_scoring"]["score_breakdown"] == {}
    assert json.loads((run.output_dir / "leads.json").read_text())[0]["lead_id"] == "lead-a"
    assert json.loads((run.output_dir / "summary.json").read_text())["businesses_discovered"] == 1
    report = run.extraction_report
    assert report["total_businesses"] == 1
    assert report["field_coverage"]["profile.services"]["not_requested"] == 1
    assert {item.name for item in run.output_dir.iterdir()} == {"leads.json", "summary.json", "scraper.log"}
    assert "[START]" in (run.output_dir / "scraper.log").read_text(encoding="utf-8")
    summary = json.loads((run.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "degraded"
    assert "warnings" not in summary and "module_statuses" not in summary
    assert summary["businesses_discovered"] == 1
    formatted_leads = (run.output_dir / "leads.json").read_text(encoding="utf-8")
    assert formatted_leads.startswith('[\n  {\n    "lead_id"')
    assert '\n    "business": {' in formatted_leads


def test_zero_must_have_coverage_degrades_run_and_preserves_nulls(tmp_path, monkeypatch, caplog):
    record = BusinessRecord.from_extracted({"business_name": "Sparse Clinic"})
    app, _, _ = setup(tmp_path, monkeypatch, records=[record])
    run = call(app, "basic")
    payload = json.loads((run.output_dir / "leads.json").read_text(encoding="utf-8"))[0]
    summary = json.loads((run.output_dir / "summary.json").read_text(encoding="utf-8"))
    assert payload["business"]["latitude"] is None
    assert payload["business"]["longitude"] is None
    assert payload["business"]["rating"] is None
    assert "review_count" not in payload["business"]
    assert summary["status"] == "degraded"
    assert "0% coverage: business.review_count" in run.summary["warnings"]
    assert "0% coverage: business.latitude" in caplog.text
    assert {item.name for item in run.output_dir.iterdir()} == {"leads.json", "summary.json", "scraper.log"}


def test_score_evidence_is_short_deduplicated_and_traceable():
    from scraper.scoring_output import compact_scoring_evidence

    repeated = {"value": "Verified service detail " * 30, "source_urls": ["https://clinic.example/services"]}
    scoring = {"factor": {"evidence_used": [repeated, repeated]}}
    compact_scoring_evidence(scoring)
    evidence = scoring["factor"]["evidence_used"]
    assert len(evidence) == 1
    assert evidence[0]["source_url"] == "https://clinic.example/services"
    assert len(evidence[0]["snippet"]) <= 200
    assert len(evidence[0]["value"]) <= 200


def test_website_does_not_run_enrichment_modules(tmp_path, monkeypatch):
    app, crawlers, _ = setup(tmp_path, monkeypatch)
    lead = call(app, "website").leads[0]
    assert len(crawlers) == 1
    assert lead.website_analysis["status"] == "active"
    assert lead.contacts is None and lead.social_links is None and lead.profile is None
    assert lead.extraction_metadata.module_status["contacts"].value == "not_requested"
    assert lead.extraction_metadata.module_status["social"].value == "not_requested"
    assert lead.extraction_metadata.module_status["technology"].value == "not_requested"
    assert lead.extraction_metadata.discovery_status == "success"
    assert lead.extraction_metadata.enrichment_status == "success"
    assert lead.extraction_metadata.overall_status == "success"


def test_contacts_only_and_failure_preserves_business(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch, fail_contacts=True)
    lead = call(app, "contacts").leads[0]
    assert lead.contacts is not None and lead.profile is None and lead.social_links is None
    assert lead.business.business_name == "Acme Studio"
    assert lead.extraction_metadata.module_status["contacts"].value == "failed"
    assert lead.extraction_metadata.extraction_status == "partial"
    assert lead.extraction_metadata.discovery_status == "success"
    assert lead.extraction_metadata.enrichment_status == "partial"
    assert lead.extraction_metadata.overall_status == "success_with_partial_enrichment"
    assert lead.extraction_metadata.module_status["business_discovery"].value == "success"


def test_no_website_preserves_discovery_and_summary_counts(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch, no_pages=True)
    run = call(app, "social")
    assert run.leads[0].business.business_name == "Acme Studio"
    assert run.leads[0].extraction_metadata.discovery_status == "success"
    assert run.leads[0].extraction_metadata.overall_status == "success_with_partial_enrichment"
    assert run.summary["businesses_discovered"] == 1
    assert run.summary["discovery_failures"] == 0
    assert run.summary["businesses_fully_enriched"] == 0
    assert run.summary["businesses_partially_enriched"] == 1
    assert run.summary["businesses_without_available_website"] == 1


def test_website_mode_complete_enrichment_and_summary(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    run = call(app, "website")
    assert run.leads[0].extraction_metadata.enrichment_status == "success"
    assert run.summary["businesses_fully_enriched"] == 1
    assert run.summary["businesses_partially_enriched"] == 0


def test_per_business_output_requires_verbose(tmp_path, monkeypatch):
    normal_messages, verbose_messages = [], []
    app, _, _ = setup(tmp_path / "normal", monkeypatch)
    app.progress = normal_messages.append
    call(app, "website")
    app, _, _ = setup(tmp_path / "verbose", monkeypatch)
    app.progress, app.verbose = verbose_messages.append, True
    call(app, "website")
    assert not any("business=lead-a" in message for message in normal_messages)
    assert any("business=lead-a" in message for message in verbose_messages)


@pytest.mark.parametrize(("mode", "module"), [("social", "social"), ("technology", "technology")])
def test_social_and_technology_independent(tmp_path, monkeypatch, mode, module):
    app, _, _ = setup(tmp_path, monkeypatch)
    lead = call(app, mode).leads[0]
    assert lead.extraction_metadata.module_status[module].value == "success"
    assert lead.extraction_metadata.module_status["contacts"].value == "not_requested"
    assert lead.extraction_metadata.module_status["profile"].value == "not_requested"
    if mode == "social":
        assert lead.social_links["instagram"]["url"] == "https://instagram.com/acme-studio"
        assert lead.website_analysis is None


def test_profile_reuses_supplied_website_result(tmp_path, monkeypatch):
    app, crawlers, _ = setup(tmp_path, monkeypatch)
    run = asyncio.run(app.run("dentists", "Nashik", mode="profile",
                              businesses=[make_business()], website_results={"lead-a": make_site()}))
    assert not crawlers
    assert run.leads[0].profile["business_description"]["value"] == "A studio serving small businesses."
    assert run.leads[0].website_analysis is None


def test_full_runs_all_available_modules_and_executes_lead_scoring(tmp_path, monkeypatch):
    app, crawlers, _ = setup(tmp_path, monkeypatch)
    lead = call(app, "full").leads[0]
    assert len(crawlers) == 1
    assert all(lead.extraction_metadata.module_status[name].value != "not_requested"
               for name in ("business_discovery", "website", "profile", "contacts", "social", "technology"))
    assert lead.extraction_metadata.module_status["lead_scoring"].value == "success"
    assert lead.lead_scoring.lead_score is not None
    assert lead.lead_scoring.priority in {"High", "Medium", "Low"}
    assert lead.lead_scoring.qualification_status in {"Qualified", "Needs Review", "Not Qualified"}
    assert lead.lead_scoring.score_breakdown["evidence_coverage"] <= 100


def test_qualification_filters_and_ranks_larger_candidate_pool(tmp_path, monkeypatch):
    records = [make_business(f"lead-{index}") for index in range(6)]
    ranked = {
        "lead-0": ("Qualified", 52), "lead-1": ("Not Qualified", 95),
        "lead-2": ("Qualified", 91), "lead-3": ("Needs Review", 80),
        "lead-4": ("Qualified", 76), "lead-5": ("Not Qualified", 20),
    }
    monkeypatch.setattr(
        "scraper.main.score_lead",
        lambda lead, config: {
            "lead_score": ranked[lead["lead_id"]][1],
            "priority": "High", "qualification_status": ranked[lead["lead_id"]][0],
            "score_breakdown": {},
        },
    )
    app, _, discovery = setup(tmp_path, monkeypatch, records=records)
    run = asyncio.run(app.run("dentists", "Nashik", 2, "full", qualification="qualified", candidate_limit=6))

    assert discovery.calls == [("dentists", "Nashik", 6)]
    assert [lead.business.lead_id for lead in run.leads] == ["lead-2", "lead-4"]
    assert run.summary["businesses_discovered"] == 6
    assert run.summary["qualification_summary"] == {
        "filter": "qualified", "candidate_limit": 6, "candidates_discovered": 6,
        "qualified_candidates": 3, "needs_review_candidates": 1,
        "not_qualified_candidates": 2, "matching_candidates": 3,
        "leads_returned": 2, "returned_order": "lead_score descending",
    }
    assert [row["lead_id"] for row in json.loads((run.output_dir / "leads.json").read_text())] == ["lead-2", "lead-4"]


def test_qualification_returns_fewer_than_limit_if_not_enough_match(tmp_path, monkeypatch):
    records = [make_business(f"lead-{index}") for index in range(3)]
    monkeypatch.setattr(
        "scraper.main.score_lead",
        lambda lead, config: {
            "lead_score": 85 if lead["lead_id"] == "lead-1" else 25,
            "priority": "Medium", "qualification_status": "Qualified" if lead["lead_id"] == "lead-1" else "Not Qualified",
            "score_breakdown": {},
        },
    )
    app, _, discovery = setup(tmp_path, monkeypatch, records=records)
    run = asyncio.run(app.run("dentists", "Nashik", 5, "full", qualification="qualified", candidate_limit=8))
    assert discovery.calls == [("dentists", "Nashik", 8)]
    assert [lead.business.lead_id for lead in run.leads] == ["lead-1"]
    assert run.summary["qualification_summary"]["matching_candidates"] == 1
    assert run.summary["qualification_summary"]["leads_returned"] == 1


def test_qualification_requires_scoring_and_valid_candidate_limit(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="lead_scoring"):
        asyncio.run(app.run("dentists", "Nashik", 5, "basic", qualification="qualified"))
    with pytest.raises(ValueError, match="candidate limit"):
        asyncio.run(app.run("dentists", "Nashik", 5, "full", qualification="qualified", candidate_limit=4))
    with pytest.raises(ValueError, match="qualification must"):
        asyncio.run(app.run("dentists", "Nashik", 5, "full", qualification="excellent"))


def test_scoring_mode_is_independent_and_does_not_crawl(tmp_path, monkeypatch):
    app, crawlers, _ = setup(tmp_path, monkeypatch)
    lead = call(app, "scoring").leads[0]
    assert not crawlers
    assert lead.extraction_metadata.module_status["lead_scoring"].value == "success"
    assert lead.extraction_metadata.module_status["website"].value == "not_requested"
    assert lead.extraction_metadata.module_status["profile"].value == "not_requested"
    assert lead.lead_scoring.score_breakdown["evidence_coverage"] < 60
    assert lead.lead_scoring.qualification_status == "Needs Review"


def test_scoring_failure_preserves_enriched_business_data(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("scraper.main.score_lead", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("score error")))
    lead = call(app, "full").leads[0]
    assert lead.business.business_name == "Acme Studio"
    assert lead.profile is not None and lead.contacts is not None
    assert lead.extraction_metadata.module_status["lead_scoring"].value == "failed"
    assert "score error" in " ".join(lead.extraction_metadata.warnings)


def test_custom_fields_request_only_dependencies_and_fields(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    lead = call(app, "custom", custom_fields=["services", "instagram"]).leads[0]
    assert lead.extraction_metadata.requested_modules == ["profile", "social"]
    assert lead.profile["services"] == []
    assert lead.profile["products"] is None
    assert lead.social_links["instagram"]["url"] == "https://instagram.com/acme-studio"
    assert lead.contacts is None and lead.website_analysis is None
    assert lead.social_links["facebook"] is None


def test_custom_validation_and_required_cli_inputs(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError): call(app, "custom")
    with pytest.raises(ValueError): call(app, "custom", custom_modules=["unsupported"])
    with pytest.raises(ValueError): asyncio.run(app.run("", "Nashik"))
    with pytest.raises(ValueError): asyncio.run(app.run("dentists", ""))
    with pytest.raises(ValueError): asyncio.run(app.run("dentists", "Nashik", 0))
    with pytest.raises(ValueError): asyncio.run(app.run("dentists", "Nashik", mode="bogus"))


def test_unavailable_site_skips_dependent_modules(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch, no_pages=True)
    lead = call(app, "social").leads[0]
    assert lead.extraction_metadata.module_status["social"].value == "skipped"
    assert lead.social_links == {"facebook": None, "instagram": None, "linkedin": None, "twitter": None}
    assert lead.extraction_metadata.warnings


def test_failed_module_does_not_erase_other_results_report_counts_match(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch, fail_contacts=True)
    run = call(app, "full")
    lead = run.leads[0]
    assert lead.profile is not None and lead.website_analysis is not None
    report = run.extraction_report
    assert report["partial"] == run.summary["partial"] == 1
    assert report["extraction_failures"][0]["failed_modules"] == ["contacts"]


def test_discovery_data_and_source_diagnostics_survive_website_failure(tmp_path, monkeypatch):
    app, _, discovery = setup(tmp_path, monkeypatch)
    discovery.records[0] = BusinessRecord.from_extracted({
        "place_id": "place-abc", "discovery_source": "google_places",
        "business_name": "Verified Dental", "category": "Dentist",
        "address": "Nashik, Maharashtra, India", "phone": "+91 12345 67890",
        "website": "https://acme.example", "rating": 4.8, "review_count": 75,
        "source_url": "https://maps.google.com/?cid=abc", "source_attributions": [{"provider": "Google"}],
    })
    discovery.diagnostics = {"source": "google_places", "fallback_used": False,
                             "original_api_failure": None, "result_count": 1}
    failed_site = WebsiteCrawlResult(lead_id=discovery.records[0].lead_id,
                                     website_status=WebsiteStatus.UNREACHABLE,
                                     website_url="https://acme.example",
                                     crawl_errors=[CrawlError(url="https://acme.example", message="HTTP 403")])
    app.website_crawler_factory = lambda config, browser: FakeCrawler(config, browser, failed_site)
    run = call(app, "full")
    payload = json.loads((run.output_dir / "leads.json").read_text(encoding="utf-8"))[0]
    assert payload["business"]["name"] == "Verified Dental"
    assert "review_count" not in payload["business"]
    assert payload["business"]["phone"] == "+911234567890"
    assert payload["business"]["rating"] == 4.8
    report = run.extraction_report
    assert report["discovery"]["source"] == "google_places"
    assert report["lead_diagnostics"][0]["place_id"] == "place-abc"
    assert report["lead_diagnostics"][0]["source_attributions"] == [{"provider": "Google"}]
    assert report["lead_diagnostics"][0]["crawl_errors"][0]["message"] == "HTTP 403"


def test_business_phone_and_contact_phone_share_normalized_format(tmp_path, monkeypatch):
    record = BusinessRecord.from_extracted({
        "lead_id": "lead-a", "business_name": "Acme Studio", "phone": "+1 (415) 555-0134",
        "website": "https://acme.example", "source_ref": "https://maps.example/acme",
    })
    app, _, _ = setup(tmp_path, monkeypatch, records=[record])
    run = call(app, "full")
    payload = json.loads((run.output_dir / "leads.json").read_text(encoding="utf-8"))[0]
    assert payload["business"]["phone"] == "+14155550134"
    assert "+14155550134" in payload["contacts"]["phone_numbers"]


def test_each_mode_has_the_same_canonical_top_level_schema(tmp_path, monkeypatch):
    expected = {"business", "profile", "website_analysis", "contacts", "social_links", "lead_scoring", "extraction_metadata"}
    for mode in ("basic", "website", "profile", "contacts", "social", "technology", "full", "scoring"):
        app, _, _ = setup(tmp_path / mode, monkeypatch)
        assert set(call(app, mode).leads[0].model_dump()) == expected


def test_serialized_contract_is_clean_and_constant_across_modes(tmp_path, monkeypatch):
    expected_top = {"lead_id", "business", "profile", "website_analysis", "contacts", "social_links", "lead_scoring"}
    expected_business = {"name", "category", "sub_category", "description", "address", "phone", "email", "website", "latitude", "longitude", "rating"}
    expected_website = {"status", "services", "about", "contact_page_url", "technology_stack"}
    expected_profile = {"services", "products", "target_customers", "about_info", "operating_hours"}
    expected_contacts = {"contact_person", "emails", "phone_numbers"}
    for mode in ("basic", "website", "profile", "contacts", "social", "technology", "full", "scoring", "custom"):
        app, _, _ = setup(tmp_path / mode, monkeypatch)
        kwargs = {"custom_fields": ["services", "instagram"]} if mode == "custom" else {}
        run = call(app, mode, **kwargs)
        leads = json.loads((run.output_dir / "leads.json").read_text(encoding="utf-8"))
        lead = leads[0]
        assert set(lead) == expected_top
        assert lead["lead_id"] == "lead-a"
        assert set(lead["business"]) == expected_business
        assert lead["business"]["name"] == "Acme Studio"
        assert set(lead["profile"]) == expected_profile
        assert set(lead["website_analysis"]) == expected_website
        assert set(lead["contacts"]) == expected_contacts
        assert set(lead["social_links"]) == {"facebook", "instagram", "linkedin"}
        if mode == "full":
            assert lead["lead_scoring"]["lead_score"] is not None
        elif mode == "scoring":
            assert lead["lead_scoring"]["qualification_status"] == "Needs Review"
        else:
            assert lead["lead_scoring"] == {
                "lead_score": None, "priority": "Unknown", "qualification_status": "Unknown"
            }
        assert set(lead["lead_scoring"]) == {"lead_score", "priority", "qualification_status"}
        assert isinstance(lead["profile"]["services"], list)
        assert isinstance(lead["profile"]["operating_hours"], dict)
        assert lead["website_analysis"]["status"] in {"not_checked", "active", "not_found", "invalid_url", "unreachable", "blocked", "uncertain"}
        assert "website_content" not in lead["website_analysis"]
        schema = run.summary["schema_validation"]
        assert schema == {"status": "valid", "schema": "LeadOutput", "validated_records": 1, "errors": []}


def test_export_omits_score_breakdown_and_keeps_internal_scoring_evidence(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    run = call(app, "full")
    payload = json.loads((run.output_dir / "leads.json").read_text(encoding="utf-8"))[0]

    assert "score_breakdown" not in payload["lead_scoring"]
    assert payload["business"]["website"] == "https://acme.example"
    # Detailed evidence remains available internally to scoring, but not in leads.json.
    assert "evidence_used" in run.leads[0].lead_scoring.score_breakdown["business_relevance"]["factors"]["category_relevance"]


def test_serialized_contract_validates_every_record_and_uses_empty_collection_defaults(tmp_path, monkeypatch):
    businesses = [make_business("lead-a"), make_business("lead-b")]
    app, _, _ = setup(tmp_path, monkeypatch, no_pages=True, records=businesses)
    run = call(app, "full")
    payload = json.loads((run.output_dir / "leads.json").read_text(encoding="utf-8"))
    assert [item["lead_id"] for item in payload] == ["lead-a", "lead-b"]
    assert all(item["profile"]["services"] == [] for item in payload)
    assert all(item["profile"]["operating_hours"] == {} for item in payload)
    assert all(item["contacts"]["emails"] == [] for item in payload)
    assert all(item["website_analysis"]["technology_stack"] == [] for item in payload)
    report = run.extraction_report
    assert report["schema_validation"]["validated_records"] == 2


def test_serializer_rejects_malformed_internal_profile_instead_of_dropping_it(tmp_path, monkeypatch):
    from scraper.main import _validate_output_payloads

    app, _, _ = setup(tmp_path, monkeypatch)
    lead = call(app, "profile").leads[0]
    lead.profile["services"] = "10+ branches"
    with pytest.raises(ValueError, match=r"lead_id=lead-a field=profile\.services expected=array of evidence objects actual=str"):
        _validate_output_payloads([lead])


def test_canonical_model_rejects_nested_ids_and_wrong_collection_types(tmp_path, monkeypatch):
    from scraper.scoring_output import LeadOutput

    app, _, _ = setup(tmp_path, monkeypatch)
    payload = json.loads((call(app, "basic").output_dir / "leads.json").read_text(encoding="utf-8"))[0]
    payload["profile"]["lead_id"] = "duplicate"
    with pytest.raises(Exception):
        LeadOutput.model_validate(payload)
    payload["profile"].pop("lead_id")
    payload["profile"]["services"] = None
    with pytest.raises(Exception):
        LeadOutput.model_validate(payload)


def test_website_services_map_to_clean_service_names(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    service_page = WebsitePage(page_url="https://acme.example/services", page_type="services",
                               main_text="Services\nRoot Canal Treatment\nDental Implants")
    site = WebsiteCrawlResult(lead_id="lead-a", website_status=WebsiteStatus.ACTIVE,
                              website_url="https://acme.example", website_content=[service_page])
    run = asyncio.run(app.run("dentists", "Nashik", mode="website", businesses=[make_business()],
                              website_results={"lead-a": site}))
    payload = json.loads((run.output_dir / "leads.json").read_text(encoding="utf-8"))[0]
    assert payload["website_analysis"]["services"] == ["Root Canal Treatment", "Dental Implants"]
    assert payload["profile"]["services"] == []
    report = run.extraction_report
    assert report["lead_diagnostics"][0]["website_content"][0]["page_url"] == service_page.page_url


def test_custom_website_and_field_aliases(tmp_path, monkeypatch):
    app, _, _ = setup(tmp_path, monkeypatch)
    lead = call(app, "custom", custom_modules=["website"]).leads[0]
    assert lead.website_analysis is not None
    with pytest.raises(ValueError):
        call(app, "custom", custom_fields=["not_a_supported_field"])
