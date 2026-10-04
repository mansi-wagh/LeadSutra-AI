from copy import deepcopy
from datetime import date

from scraper.scoring import ScoringConfig, classify_score, score_lead


def full_lead():
    page_text = "Northstar Dental provides family dentistry and restorative care. " * 12
    return {
        "lead_id": "lead-1",
        "business": {
            "business_name": "Northstar Dental", "category": "Dental Clinic",
            "description": "Independent clinic providing dental care.", "address": "Pune, India",
            "phone": "+91 98765 43210", "email": "hello@northstar.example",
            "website": "https://northstar.example", "rating": 4.8, "review_count": 620,
        },
        "profile": {
            "services": ["Family dentistry", "Restorative care"],
            "products": ["Dental kits", "Whitening kits", "Care plans"],
            "target_customers": ["families", "adults"],
            "about_info": "Northstar Dental was founded in 2010 and serves Pune families. Our team of 12 professionals provides care.",
            "business_description": "Independent clinic providing dental care to Pune residents.",
            "operating_hours": {"Monday": {"opens": "09:00", "closes": "17:00", "closed": False}},
        },
        "website_analysis": {
            "status": "active", "website_url": "https://northstar.example",
            "about": "Northstar Dental serves Pune families.",
            "services": ["Family dentistry", "Restorative care"],
            "contact_page_url": "https://northstar.example/contact-us", "technology_stack": [],
            "pages_visited": ["https://northstar.example/", "https://northstar.example/about"],
            "website_content": [
                {"page_url": "https://northstar.example/", "page_title": "Northstar Dental",
                 "meta_description": "Dental clinic in Pune.", "main_text": page_text,
                 "technology_signals": ["meta-viewport:width=device-width, initial-scale=1"]},
                {"page_url": "https://northstar.example/about", "page_title": "About Northstar",
                 "meta_description": "About our dental practice.", "main_text": page_text,
                 "technology_signals": []},
            ],
        },
        "contacts": {
            "contact_person": None, "emails": ["hello@northstar.example"],
            "phone_numbers": ["+919876543210"], "contact_page_url": "https://northstar.example/contact-us",
        },
        "social_links": {
            "facebook": "https://facebook.com/northstar-dental",
            "instagram": "https://instagram.com/northstar.dental",
            "linkedin": "https://linkedin.com/company/northstar-dental",
            "twitter": "https://x.com/northstardental",
        },
        "extraction_metadata": {
            "source": "google_places",
            "module_status": {name: "success" for name in
                               ("profile", "website", "contacts", "social")},
            "field_sources": {
                "business.category": ["https://maps.example/place/1"],
                "profile.services": ["https://northstar.example/services"],
                "profile.products": ["https://northstar.example/products"],
                "profile.target_customers": ["https://northstar.example/about"],
                "social.facebook": ["https://northstar.example/"],
                "social.instagram": ["https://northstar.example/"],
                "social.linkedin": ["https://northstar.example/"],
                "social.twitter": ["https://northstar.example/"],
            },
        },
    }


def test_complete_evidence_normalizes_to_one_hundred_and_is_deterministic():
    lead = full_lead()
    config = ScoringConfig(reference_year=2026)
    first = score_lead(lead, config)
    assert first == score_lead(deepcopy(lead), config)
    assert first["lead_score"] == 100
    assert first["priority"] == "High"
    assert first["qualification_status"] == "Qualified"
    assert first["score_breakdown"]["evidence_coverage"] == 100
    assert first["score_breakdown"]["business_relevance"]["maximum_points"] == 20
    assert first["score_breakdown"]["business_maturity"]["earned_points"] == 15
    assert first["score_breakdown"]["business_relevance"]["factors"]["service_relevance"]["evidence_used"][0]["source_urls"] == ["https://northstar.example/services"]
    assert first["score_breakdown"]["business_reputation"]["factors"]["review_volume"]["earned_points"] == 5


def test_review_volume_uses_actual_count_and_missing_count_is_not_zero_reviews():
    lead = full_lead()
    lead["business"]["review_count"] = 84
    factor = score_lead(lead)["score_breakdown"]["business_reputation"]["factors"]["review_volume"]
    assert factor["earned_points"] == 3
    assert factor["evidence_used"][0]["value"] == "84 reviews"

    lead["business"]["review_count"] = None
    factor = score_lead(lead)["score_breakdown"]["business_reputation"]["factors"]["review_volume"]
    assert factor["earned_points"] == 0
    assert factor["assessable_weight"] == 0
    assert factor["evidence_used"] == []


def test_blocked_website_and_missing_data_are_unavailable_not_zero_quality():
    lead = full_lead()
    lead["website_analysis"].update({"status": "blocked", "website_content": [], "pages_visited": []})
    lead["business"].update({"website": None, "rating": None, "review_count": None, "phone": None, "email": None})
    lead["contacts"].update({"emails": [], "phone_numbers": [], "contact_page_url": None})
    result = score_lead(lead, ScoringConfig(reference_year=2026))
    website = result["score_breakdown"]["website_quality"]
    assert website["factors"]["website_availability"]["status"] == "unavailable"
    assert website["factors"]["website_content_quality"]["assessable_weight"] == 0
    assert result["score_breakdown"]["business_reputation"]["assessable_weight"] == 0
    assert result["score_breakdown"]["contact_accessibility"]["factors"]["valid_business_phone"]["status"] == "assessed"


def test_invalid_contacts_and_no_social_profiles_are_assessed_without_points():
    lead = full_lead()
    lead["business"].update({"phone": "1111111", "email": "not-an-email"})
    lead["contacts"].update({"phone_numbers": ["1111111"], "emails": ["not-an-email"]})
    lead["social_links"] = {"facebook": None, "instagram": None, "linkedin": None, "twitter": None}
    lead["extraction_metadata"]["module_status"]["social"] = "not_available"
    result = score_lead(lead, ScoringConfig(reference_year=2026))
    factors = result["score_breakdown"]["contact_accessibility"]["factors"]
    assert factors["valid_business_phone"]["earned_points"] == 0
    assert factors["valid_business_phone"]["missing_or_unavailable"] == ["Phone evidence present but invalid"]
    assert factors["valid_business_email"]["earned_points"] == 0
    assert result["score_breakdown"]["digital_presence"]["factors"]["official_social_profiles"]["assessable_weight"] == 8


def test_low_coverage_keeps_high_provisional_score_but_requires_review():
    lead = full_lead()
    lead["profile"] = None
    lead["website_analysis"] = None
    lead["contacts"] = None
    lead["social_links"] = None
    lead["business"].update({"phone": None, "email": None, "website": None, "rating": 5.0, "review_count": 900})
    lead["extraction_metadata"]["module_status"] = {}
    result = score_lead(lead, ScoringConfig(reference_year=2026))
    assert result["lead_score"] == 100
    assert result["score_breakdown"]["evidence_coverage"] < 60
    assert result["priority"] == "High"
    assert result["qualification_status"] == "Needs Review"


def test_explicit_size_and_history_evidence_score_but_missing_evidence_is_not_assumed():
    lead = full_lead()
    lead["website_analysis"]["website_content"][0]["main_text"] += " Our team of 12 has served patients since 2010."
    scored = score_lead(lead, ScoringConfig(reference_year=2026))
    maturity = scored["score_breakdown"]["business_maturity"]["factors"]
    assert maturity["team_or_business_scale"]["earned_points"] == 5
    assert maturity["operating_history"]["earned_points"] == 5
    unknown = full_lead()
    unknown["website_analysis"]["website_content"] = []
    unknown["profile"]["about_info"] = "Northstar Dental is a dental practice serving Pune."
    unknown["extraction_metadata"]["module_status"]["profile"] = "skipped"
    unknown["extraction_metadata"]["module_status"]["website"] = "failed"
    unknown_score = score_lead(unknown, ScoringConfig(reference_year=2026))
    unknown_factors = unknown_score["score_breakdown"]["business_maturity"]["factors"]
    assert unknown_factors["team_or_business_scale"]["status"] == "unavailable"
    assert unknown_factors["operating_history"]["status"] == "unavailable"


def test_configurable_score_band_boundaries():
    expected = {
        39: ("Low", "Not Qualified"), 40: ("Low", "Needs Review"),
        59: ("Low", "Needs Review"), 60: ("Medium", "Qualified"),
        79: ("Medium", "Qualified"), 80: ("High", "Qualified"), 100: ("High", "Qualified"),
    }
    for score, result in expected.items():
        assert classify_score(score, 60) == result
    assert classify_score(90, 59.9) == ("High", "Needs Review")
