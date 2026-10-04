from pathlib import Path
from urllib.parse import urlparse

import asyncio
import httpx
import pytest

from scraper.models import BusinessRecord
from scraper.website import (
    WebsiteCrawler, WebsiteCrawlerConfig, WebsiteStatus, _clean_page, normalize_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def business(url="https://acme.example"):
    return BusinessRecord.from_extracted({"business_name": "Acme", "website": url})


class MockResponse:
    def __init__(self, url, text, status_code=200, content_type="text/html"):
        self.url = httpx.URL(url)
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("failed", request=httpx.Request("GET", str(self.url)), response=httpx.Response(self.status_code))


class MockClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.closed = False
    async def get(self, url):
        self.calls.append(url)
        response = self.pages.get(url)
        if isinstance(response, Exception):
            raise response
        if response is None:
            response = MockResponse(url, "", 404)
        response.raise_for_status()
        return response
    async def aclose(self): self.closed = True


def test_url_normalization_and_invalid_urls():
    assert normalize_url(" acme.example/path/?utm_source=directory ") == "https://acme.example/path"
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("https://bad host") is None


def test_empty_and_noisy_html_cleanup():
    page, links = _clean_page('<html><head><title>Empty</title></head><body><nav>Repeated</nav><script>x</script><div class="cookie-banner">noise</div></body></html>', "https://acme.example")
    assert page.main_text == ""
    assert page.page_title == "Empty"
    assert links == []


def test_none_html_and_null_attributes_are_parsed_defensively():
    page, links = _clean_page(None, "https://acme.example")
    assert page.main_text == ""
    assert links == []
    page, _ = _clean_page('<html class=""><body><a href="">empty</a><main><p>Useful details.</p><div class="cookie">x</div></main></body></html>', "https://acme.example")
    assert page.main_text == "Useful details."


def test_cleanup_skips_descendants_of_removed_noisy_elements():
    html = '<html><body><div class="cookie-banner"><div id="nested"><p>Noise</p></div></div><main><p>Useful clinic details.</p></main></body></html>'
    page, _ = _clean_page(html, "https://ramoledental.com/")
    assert "Useful clinic details." in page.main_text
    assert "Noise" not in page.main_text


def test_discovery_parser_rejects_none_html_and_malformed_card_attributes():
    from scraper.parsing import parse_business_results

    assert parse_business_results(None) == []
    records = parse_business_results('<div role="article"><a href="/maps/place/acme" aria-label="Acme"></a><a href="tel:"></a></div>')
    assert len(records) == 1
    assert records[0].business_name == "Acme"


def test_social_candidate_is_uncertain_without_fetch():
    client = MockClient({})
    result = asyncio.run(WebsiteCrawler(client=client).crawl(business("https://www.facebook.com/acme")))
    assert result.website_status == WebsiteStatus.UNCERTAIN
    assert client.calls == []


def test_missing_and_invalid_site_statuses():
    missing = BusinessRecord.from_extracted({"business_name": "No Site"})
    assert asyncio.run(WebsiteCrawler().crawl(missing)).website_status == WebsiteStatus.NOT_FOUND
    assert asyncio.run(WebsiteCrawler().crawl(business("http://"))).website_status == WebsiteStatus.INVALID_URL


def test_static_crawl_redirect_dedup_and_page_limit():
    home = (FIXTURES / "static_site_home.html").read_text(encoding="utf-8")
    about = (FIXTURES / "static_site_about.html").read_text(encoding="utf-8")
    services = (FIXTURES / "static_site_services.html").read_text(encoding="utf-8")
    pages = {
        "https://acme.example/": MockResponse("https://www.acme.example/", home),
        "https://www.acme.example/about": MockResponse("https://www.acme.example/about", about),
        "https://www.acme.example/services": MockResponse("https://www.acme.example/services", services),
    }
    client = MockClient(pages)
    result = asyncio.run(WebsiteCrawler(WebsiteCrawlerConfig(max_pages=2), client=client).crawl(business()))
    assert result.website_status == WebsiteStatus.ACTIVE
    assert result.final_url == "https://www.acme.example/"
    assert len(result.pages_visited) == 2
    assert len(set(result.pages_visited)) == 2
    assert "tracking" not in result.website_content[0].main_text
    assert "cookie" not in result.website_content[0].main_text.lower()
    assert all("cart" not in url for url in result.pages_visited)
    assert "https://www.acme.example/" in result.field_sources["website_content"]
    assert client.closed is False


def test_unreachable_and_http_timeout():
    timeout = httpx.ReadTimeout("timed out", request=httpx.Request("GET", "https://acme.example"))
    client = MockClient({"https://acme.example/": timeout})
    result = asyncio.run(WebsiteCrawler(client=client).crawl(business()))
    assert result.website_status == WebsiteStatus.UNREACHABLE
    assert result.crawl_errors


@pytest.mark.parametrize("failure", [
    httpx.HTTPStatusError("403 Forbidden", request=httpx.Request("GET", "https://acme.example/"), response=httpx.Response(403)),
    httpx.ConnectError("DNS resolution failed", request=httpx.Request("GET", "https://acme.example/")),
])
def test_forbidden_and_dns_failures_are_website_level(failure):
    result = asyncio.run(WebsiteCrawler(client=MockClient({"https://acme.example/": failure})).crawl(business()))
    expected = WebsiteStatus.BLOCKED if isinstance(failure, httpx.HTTPStatusError) else WebsiteStatus.UNREACHABLE
    assert result.website_status == expected
    assert result.crawl_errors


def test_browser_fallback_for_javascript_page():
    class FakePage:
        async def goto(self, url, **kwargs): self.url = url
        async def content(self): return "<html><main><h1>Rendered</h1><p>JavaScript rendered business content with enough text.</p></main></html>"
    class FakeManager:
        def __init__(self, config): self.page = FakePage(); self.closed = False
        async def start(self): return self.page
        async def close(self): self.closed = True
    html = '<html><head><script src="app.js"></script></head><body><div id="app"></div></body></html>'
    client = MockClient({"https://acme.example/": MockResponse("https://acme.example/", html)})
    manager_instances = []
    def factory(config):
        manager = FakeManager(config); manager_instances.append(manager); return manager
    crawler = WebsiteCrawler(WebsiteCrawlerConfig(min_text_for_static=30), client=client, browser_manager_factory=factory)
    result = asyncio.run(crawler.crawl(business()))
    assert result.website_content[0].main_text == "Rendered\nJavaScript rendered business content with enough text."
    assert manager_instances[0].closed is True


def test_config_validates_max_pages():
    with pytest.raises(ValueError):
        WebsiteCrawlerConfig(max_pages=0)


def test_duplicate_links_and_site_without_relevant_pages():
    home = '<html><main><h1>Local firm</h1><p>General information for customers.</p><a href="/privacy">Privacy</a><a href="/privacy/">Privacy duplicate</a><a href="https://other.example/about">External</a></main></html>'
    privacy = '<html><main><h1>Privacy policy</h1><p>Privacy terms and data use information.</p></main></html>'
    client = MockClient({
        "https://acme.example/": MockResponse("https://acme.example/", home),
        "https://acme.example/privacy": MockResponse("https://acme.example/privacy", privacy),
    })
    result = asyncio.run(WebsiteCrawler(WebsiteCrawlerConfig(max_pages=5), client=client).crawl(business()))
    assert result.website_status == WebsiteStatus.ACTIVE
    assert client.calls.count("https://acme.example/privacy") == 1
    assert all("other.example" not in url for url in client.calls)
    assert result.about == []
    assert result.services == []


@pytest.mark.parametrize("contact_path", ["contact", "contact-us", "get-in-touch", "reach-out"])
def test_contact_page_aliases_are_discovered_and_prioritized(contact_path):
    home = f'<html><body><main><h1>Acme</h1><a href="/{contact_path}">Get in touch</a></main></body></html>'
    contact_url = f"https://acme.example/{contact_path}"
    contact = '<html><head><title>Contact Acme</title></head><body><main><h1>Contact</h1><p>Email info@acme.example</p></main></body></html>'
    client = MockClient({
        "https://acme.example/": MockResponse("https://acme.example/", home),
        contact_url: MockResponse(contact_url, contact),
    })
    result = asyncio.run(WebsiteCrawler(WebsiteCrawlerConfig(max_pages=3), client=client).crawl(business()))
    assert result.contact_page_url == contact_url
    assert contact_url in result.pages_visited


def test_browser_timeout_is_recorded_and_static_result_kept():
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    class FakePage:
        async def goto(self, *args, **kwargs): raise PlaywrightTimeout("render timeout")
        async def content(self): raise AssertionError("content should not be requested after timeout")
    class FakeManager:
        def __init__(self, config): self.page = FakePage(); self.closed = False
        async def start(self): return self.page
        async def close(self): self.closed = True
    html = '<html><head><script src="client.js"></script></head><body><main><h1>Basic</h1><p>Static text retained during render timeout.</p></main></body></html>'
    manager_instances = []
    def factory(config):
        manager = FakeManager(config); manager_instances.append(manager); return manager
    client = MockClient({"https://acme.example/": MockResponse("https://acme.example/", html)})
    crawler = WebsiteCrawler(WebsiteCrawlerConfig(min_text_for_static=1000), client=client, browser_manager_factory=factory)
    result = asyncio.run(crawler.crawl(business()))
    assert result.website_status == WebsiteStatus.ACTIVE
    assert "Static text retained" in result.website_content[0].main_text
    assert any("Playwright" in error.message for error in result.crawl_errors)
    assert manager_instances[0].closed is True
