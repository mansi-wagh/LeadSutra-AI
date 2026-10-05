from scraper.enrichment import extract_contacts
from scraper.enrichment import extract_all, extract_requested
from scraper.discovery import BusinessRecord
from scraper.enrichment import extract_social_media
from scraper.enrichment import detect_technologies
from scraper.enrichment import WebsiteCrawlResult, WebsiteLink, WebsitePage, WebsiteStatus, _clean_page


def business():
    return BusinessRecord.from_extracted({"business_name": "Acme Studio"})


def result(*pages, contact_url=None):
    return WebsiteCrawlResult(
        lead_id=business().lead_id,
        website_status=WebsiteStatus.ACTIVE,
        website_content=list(pages),
        contact_page_url=contact_url,
    )


def test_emails_deduplicate_filter_placeholders_and_keep_sources():
    home = WebsitePage(
        page_url="https://acme.example/",
        main_text="Contact info@acme.example and SALES@Acme.example. Example test@example.com; noreply@acme.example.",
    )
    contact = WebsitePage(
        page_url="https://acme.example/contact", page_type="contact", main_text="sales@acme.example",
        outgoing_links=[WebsiteLink(url="mailto:hello@acme.example", text="Email us")],
    )
    extracted = extract_contacts(business(), result(home, contact, contact_url=contact.page_url))
    assert [item.email for item in extracted.contact_emails] == ["info@acme.example", "sales@acme.example", "hello@acme.example"]
    sales = next(item for item in extracted.contact_emails if item.email == "sales@acme.example")
    assert sales.source_urls == [home.page_url, contact.page_url]
    assert extracted.email == "info@acme.example"
    assert extracted.contact_page_url == contact.page_url


def test_contact_person_only_when_explicit_and_multiple_phones_deduplicate():
    first = WebsitePage(
        page_url="https://acme.example/about",
        main_text="Contact person: Jane Smith\nCall +1 (415) 555-0134 or 415-555-0134.",
    )
    second = WebsitePage(page_url="https://acme.example/contact", main_text="Phone: +44 20 7946 0958")
    extracted = extract_contacts(business(), result(first, second))
    assert extracted.contact_person.value == "Jane Smith"
    assert extracted.contact_person.source_urls == [first.page_url]
    assert len(extracted.phone_numbers) == 2
    assert extracted.phone_numbers[0].source_urls == [first.page_url]
    assert extracted.phone == "+14155550134"


def test_contact_not_guessed_and_contact_url_can_be_missing():
    page = WebsitePage(page_url="https://acme.example/", main_text="Founded by Jane Smith.")
    extracted = extract_contacts(business(), result(page))
    assert extracted.contact_person is None
    assert extracted.contact_page_url is None


def test_footer_contacts_are_retained_with_page_provenance():
    page, _ = _clean_page(
        '<html><body><main><h1>Acme</h1><p>Design studio services for local companies.</p></main>'
        '<footer><p>Contact us: footer@acme.example | Call +1 (415) 555-0134</p></footer></body></html>',
        "https://acme.example/",
    )
    assert "footer@acme.example" in page.contact_text
    found = extract_contacts(business(), result(page))
    assert found.contact_emails[0].email == "footer@acme.example"
    assert found.contact_emails[0].source_urls == [page.page_url]
    assert found.phone_numbers[0].normalized == "+14155550134"
    assert found.phone == "+14155550134"


def test_social_links_validate_match_name_and_preserve_source():
    page = WebsitePage(
        page_url="https://acme.example/",
        outgoing_links=[
            WebsiteLink(url="https://instagram.com/acme.studio/?utm_source=site", text="Instagram"),
            WebsiteLink(url="https://facebook.com/unrelated-company", text="Facebook"),
            WebsiteLink(url="https://linkedin.com/company/acme-studio/", text="LinkedIn"),
            WebsiteLink(url="javascript:alert(1)", text="X"),
            WebsiteLink(url="https://x.com/acme_studio", text="X"),
            WebsiteLink(url="https://facebook.com/acme-studio", text="Facebook"),
        ],
    )
    found = extract_social_media(business(), result(page))
    assert found.instagram.url == "https://instagram.com/acme.studio"
    assert found.linkedin.url == "https://linkedin.com/company/acme-studio"
    assert found.twitter.url == "https://x.com/acme_studio"
    assert found.facebook.url == "https://facebook.com/acme-studio"
    assert found.field_sources["instagram"] == [page.page_url]


def test_social_detector_rejects_share_and_content_links():
    page = WebsitePage(page_url="https://acme.example/", outgoing_links=[
        WebsiteLink(url="https://www.facebook.com/sharer.php?u=https://acme.example", text="Facebook"),
        WebsiteLink(url="https://instagram.com/acme/reel/12345", text="Instagram"),
        WebsiteLink(url="https://x.com/acme/status/12345", text="X"),
        WebsiteLink(url="https://linkedin.com/company/acme-studio", text="LinkedIn"),
    ])
    found = extract_social_media(business(), result(page))
    assert found.facebook is None and found.instagram is None and found.twitter is None
    assert found.linkedin.url == "https://linkedin.com/company/acme-studio"


def test_technology_signatures_and_no_false_claims():
    page = WebsitePage(
        page_url="https://acme.example/",
        technology_signals=[
            "meta-generator:WordPress 6.5",
            "script-src:https://www.googletagmanager.com/gtag/js?id=G-123",
            "html-signature:__next_data__",
            "script-src:https://cdn.shopify.com/s/files/shop.js",
            "meta-generator:Wix",
            "script-src:https://cdn.example.test/react-dom.production.min.js",
        ],
    )
    detected = detect_technologies(result(page))
    names = [item.name for item in detected.technologies]
    assert names == ["Google Analytics", "Next.js", "React", "Shopify", "Wix", "WordPress"]
    assert all(item.source_urls == [page.page_url] for item in detected.technologies)
    assert detect_technologies(result(WebsitePage(page_url="https://acme.example/"))).technologies == []


def test_technology_mentions_and_external_links_are_not_technical_evidence():
    page, _ = _clean_page(
        '<html><body><main><p>We like WordPress, Shopify, React, and Google Analytics.</p>'
        '<a href="https://cdn.shopify.com/widgets/example">Partner asset</a></main></body></html>',
        "https://acme.example/",
    )
    assert detect_technologies(result(page)).technologies == []


def test_only_actual_generator_scripts_and_headers_create_technology_evidence():
    page, _ = _clean_page(
        '<html><head><meta name="generator" content="WordPress 6">'
        '<script src="https://www.googletagmanager.com/gtag/js?id=G-1"></script></head>'
        '<body><main><p>Business content here.</p></main></body></html>',
        "https://acme.example/",
    )
    found = detect_technologies(result(page))
    assert {item.name for item in found.technologies} == {"WordPress", "Google Analytics"}
    assert all(item.source_urls == [page.page_url] for item in found.technologies)


def test_existing_crawler_page_retains_link_and_technology_evidence():
    page, _ = _clean_page(
        '<html><head><meta name="generator" content="WordPress 6"><script src="/wp-includes/js/app.js"></script></head>'
        '<body><main><p>Call 415 555 0100</p><a href="mailto:hello@acme.example">Email</a>'
        '<a href="https://instagram.com/acme-studio">Instagram</a></main></body></html>',
        "https://acme.example/",
    )
    assert any(link.url == "mailto:hello@acme.example" for link in page.outgoing_links)
    assert any("instagram.com/acme-studio" in link.url for link in page.outgoing_links)
    assert "meta-generator:WordPress 6" in page.technology_signals
    assert "script-src:/wp-includes/js/app.js" in page.technology_signals


def test_selective_and_combined_execution():
    page = WebsitePage(page_url="https://acme.example/", main_text="info@acme.example")
    site = result(page)
    only_technology = extract_requested(business(), site, technology=True)
    assert only_technology.technology is not None
    assert only_technology.contacts is None
    assert only_technology.social is None
    all_results = extract_all(business(), site)
    assert all_results.contacts is not None
    assert all_results.social is not None
    assert all_results.technology is not None
