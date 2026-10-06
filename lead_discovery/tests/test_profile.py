import pytest

from scraper.discovery import BusinessRecord
from scraper.enrichment import build_business_profile
from scraper.enrichment import WebsiteCrawlResult, WebsitePage, WebsiteStatus


def business():
    return BusinessRecord.from_extracted({
        "business_name": "Northstar Studio",
        "description": "Brand design studio for growing companies.",
        "source_ref": "https://directory.example/listing/northstar",
    })


def website(*pages):
    return WebsiteCrawlResult(
        lead_id=business().lead_id,
        website_status=WebsiteStatus.ACTIVE,
        website_content=list(pages),
    )


def test_complete_evidence_based_profile_and_sources():
    home_url = "https://northstar.example/"
    about_url = "https://northstar.example/about"
    services_url = "https://northstar.example/services"
    products_url = "https://northstar.example/products"
    home = WebsitePage(
        page_url=home_url, page_type="home",
        page_title="Northstar Studio",
        meta_description="Independent brand design studio serving local companies.",
        main_text="Northstar Studio\nWe serve small businesses and nonprofit organizations.\nMonday-Friday: 9:00 AM - 5:00 PM\nSaturday: Closed",
    )
    about = WebsitePage(
        page_url=about_url, page_type="about",
        main_text="Our Story\nFounded in 2012, Northstar Studio works with local organizations.",
    )
    services = WebsitePage(
        page_url=services_url, page_type="services",
        main_text="Services\nBrand strategy\nGraphic design\nWe provide web design and brand strategy.",
    )
    products = WebsitePage(
        page_url=products_url, page_type="products",
        main_text="Products\nProducts include printed stationery and brand templates.",
    )
    profile = build_business_profile(business(), website(home, about, services, products))
    assert [item.value for item in profile.services] == ["Brand strategy", "Graphic design", "web design"]
    assert [item.value for item in profile.products] == ["printed stationery", "brand templates"]
    assert profile.target_customers[0].value == "small businesses and nonprofit organizations"
    assert profile.business_description.value == "Independent brand design studio serving local companies."
    assert profile.business_description.source_urls == [home_url]
    assert {hour.day for hour in profile.operating_hours} == {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"}
    saturday = next(hour for hour in profile.operating_hours if hour.day == "Saturday")
    assert saturday.closed is True and saturday.opens is None
    assert profile.field_sources["services"] == [services_url]
    assert profile.about_info[0].source_urls == [home_url]


def test_partial_pages_missing_about_hours_and_unclear_products():
    service_url = "https://example.test/services"
    services = WebsitePage(page_url=service_url, page_type="services", main_text="Services\nConsulting\nImplementation")
    generic = WebsitePage(page_url="https://example.test/faq", page_type="faq", main_text="Frequently asked questions\nWe help you get started.")
    profile = build_business_profile(business(), website(services, generic))
    assert [item.value for item in profile.services] == ["Consulting", "Implementation"]
    assert profile.products == []
    assert profile.about_info == []
    assert profile.operating_hours == []
    assert profile.business_description.value == "Brand design studio for growing companies."
    assert profile.business_description.source_urls == ["https://directory.example/listing/northstar"]
    assert profile.target_customers == []


def test_empty_content_and_missing_services_page():
    page = WebsitePage(page_url="https://example.test/", page_type="home", main_text="  ")
    profile = build_business_profile(business(), website(page))
    assert profile.services == []
    assert profile.products == []
    assert profile.target_customers == []
    assert profile.about_info == []
    assert profile.business_description.value == "Brand design studio for growing companies."
    assert profile.business_description.source_urls == ["https://directory.example/listing/northstar"]
    assert profile.operating_hours == []


def test_duplicate_services_merge_sources_and_normalize_whitespace():
    first = WebsitePage(page_url="https://example.test/services", page_type="services", main_text="Services\nAI assistant setup")
    second = WebsitePage(page_url="https://example.test/solutions", page_type="services", main_text="Our Services\n  AI   assistant setup")
    profile = build_business_profile(business(), website(first, second))
    assert len(profile.services) == 1
    assert profile.services[0].value == "AI assistant setup"
    assert profile.services[0].source_urls == [first.page_url, second.page_url]


def test_explicit_customer_segments_only_and_matching_lead_id():
    page = WebsitePage(page_url="https://example.test/", page_type="home", main_text="We serve independent retailers.")
    profile = build_business_profile(business(), website(page))
    assert [fact.value for fact in profile.target_customers] == ["independent retailers"]
    mismatched = WebsiteCrawlResult(lead_id="wrong", website_status=WebsiteStatus.ACTIVE)
    with pytest.raises(ValueError):
        build_business_profile(business(), mismatched)


def test_customer_categories_are_constrained_and_services_filter_ui_fragments():
    page = WebsitePage(
        page_url="https://acme.example/services", page_type="services",
        main_text=("Services\n10+\nBranches\nfor teeth\nDental Implants\nRoot Canal Treatment\n"
                   "We serve families and small businesses who want to achieve better outcomes."),
    )
    profile = build_business_profile(business(), website(page))
    assert [fact.value for fact in profile.services] == ["Dental Implants", "Root Canal Treatment"]
    assert [fact.value for fact in profile.target_customers] == ["families"]


def test_meaningful_explicit_customer_group_is_retained():
    page = WebsitePage(page_url="https://acme.example/", page_type="home",
                       main_text="We serve children, adults, and families.")
    profile = build_business_profile(business(), website(page))
    assert [fact.value for fact in profile.target_customers] == ["children", "adults", "families"]


def test_hours_unavailable_is_empty_not_closed():
    page = WebsitePage(page_url="https://example.test/contact", page_type="contact", main_text="Contact us for opening hours.")
    profile = build_business_profile(business(), website(page))
    assert profile.operating_hours == []
