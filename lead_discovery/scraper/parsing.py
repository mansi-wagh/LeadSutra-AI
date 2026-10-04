from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .models import BusinessRecord

_NUMBER = re.compile(r"[\d,.]+")
_LL = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_PHONE_LIKE = re.compile(r"(?:\+?\d[\d\s().-]{5,}\d)")
_RATING_LIKE = re.compile(r"^\s*\d(?:[.,]\d)?\s*(?:\(.*\))?\s*$")
_NOISE_LINE = re.compile(r"\b(?:open|closed|closes|opens|hours|directions|website|reviews?|\$+|km|mi)\b", re.I)


def parse_latlon(url: str | None) -> tuple[float | None, float | None]:
    """Recover validated India coordinates from a Google Maps place URL."""
    match = _LL.search(url or "")
    if not match:
        return None, None
    latitude, longitude = map(float, match.groups())
    if 6 <= latitude <= 38 and 68 <= longitude <= 98:
        return latitude, longitude
    return None, None


def parse_reviews(text: str | None) -> int | None:
    """Parse an explicit review count without mistaking the rating for the count."""
    text = text or ""
    match = re.search(r"([\d,]+)\s+reviews?\b", text, re.I)
    if match:
        return int(match.group(1).replace(",", ""))
    # Some Maps views render the compact form `4.3(1,240)`.
    match = re.search(r"\b[0-5](?:\.\d+)?\s*(?:stars?\s*)?\(\s*([\d,]+)\s*\)", text, re.I)
    return int(match.group(1).replace(",", "")) if match else None


def _attribute_value(card: Tag, *, item_ids: tuple[str, ...], labels: tuple[str, ...]) -> str | None:
    """Read a value only from Maps' explicit accessible/data-item annotations."""
    for node in card.find_all(True):
        if not isinstance(node, Tag):
            continue
        item_id = node.get("data-item-id")
        if isinstance(item_id, str) and any(item_id.casefold().startswith(item.casefold()) for item in item_ids):
            value = _text(node)
            if value:
                return value
        for attr in ("aria-label", "title", "data-tooltip"):
            label = node.get(attr)
            if not isinstance(label, str):
                continue
            match = re.match(r"\s*(?:" + "|".join(re.escape(item) for item in labels) + r")\s*:\s*(.+?)\s*$", label, re.I)
            if match:
                return match.group(1).strip()
    return None


def _candidate_category(card: Tag, name: str) -> str | None:
    explicit = _attribute_value(card, item_ids=("category",), labels=("category", "business category"))
    if explicit:
        return explicit
    # Maps cards commonly render category as a short standalone text fragment.
    # Only accept a fragment when it is distinguishable from listing metadata.
    for fragment in card.stripped_strings:
        value = re.sub(r"\s+", " ", str(fragment)).strip(" ,|")
        value = re.sub(r"^[^\w]+|[^\w]+$", "", value)
        if (not value or value.casefold() == name.casefold() or len(value) > 65
                or _RATING_LIKE.fullmatch(value) or _PHONE_LIKE.search(value)
                or "@" in value or "http" in value or _NOISE_LINE.search(value)
                or re.search(r"\d{1,2}:\d{2}|\b\d+\s+\w+\s+(?:street|road|rd|ave|avenue|lane|highway)\b", value, re.I)):
            continue
        if re.search(r"\b(?:store|shop|clinic|restaurant|cafe|hotel|agency|school|dentist|salon|hospital|service|contractor|market|company|center|centre|marketing|consultant|builder|repair|hardware|pharmacy|lawyer|accountant)\b", value, re.I):
            return value
    return None


def _candidate_address(card: Tag) -> str | None:
    explicit = _attribute_value(card, item_ids=("address",), labels=("address",))
    if explicit:
        return explicit
    # Address fragments in Maps result cards are often separate text nodes;
    # require a street/locality indicator or multiple comma-separated parts.
    for fragment in card.stripped_strings:
        value = re.sub(r"\s+", " ", str(fragment)).strip(" ,|")
        value = re.sub(r"^[^\w]+|[^\w]+$", "", value)
        if (not value or len(value) > 180 or "@" in value or "http" in value
                or _PHONE_LIKE.search(value) or _RATING_LIKE.fullmatch(value)
                or _NOISE_LINE.search(value)):
            continue
        if (re.search(r"\b(?:street|road|rd\.?|avenue|ave\.?|lane|highway|sector|nagar|colony|building|floor|plot|opp(?:osite)?|near)\b", value, re.I)
                or value.count(",") >= 2):
            return value
    return None


def _text(node: Tag | None) -> str | None:
    if node is None:
        return None
    value = " ".join(node.stripped_strings)
    return value or None


def _parse_listing(card: Tag, page_url: str) -> BusinessRecord | None:
    if not isinstance(card, Tag):
        return None
    link = card.select_one('a[href*="/maps/place/"]')
    source_url = link.get("href") if isinstance(link, Tag) else None
    source_url = source_url if isinstance(source_url, str) else None
    if source_url and source_url.startswith("/"):
        source_url = "https://www.google.com" + source_url
    name = link.get("aria-label") if isinstance(link, Tag) else None
    name = name if isinstance(name, str) else None
    name = (name or _text(card.select_one(".fontHeadlineSmall")) or "").strip()
    if not name:
        return None

    rating = None
    rating_node = card.select_one('[aria-label*="star"], [aria-label*="rating"]')
    rating_text = rating_node.get("aria-label") if isinstance(rating_node, Tag) else ""
    rating_text = rating_text if isinstance(rating_text, str) else ""
    match = _NUMBER.search(rating_text)
    if match:
        try:
            rating = float(match.group().replace(",", ""))
        except ValueError:
            pass

    review_texts = []
    for node in card.find_all(True):
        for attr in ("aria-label", "title"):
            value = node.get(attr)
            if isinstance(value, str) and re.search(r"review|star|rating|\([\d,]+\)", value, re.I):
                review_texts.append(value)
    review_texts.extend(str(fragment) for fragment in card.stripped_strings)
    review_count = next((count for text in review_texts if (count := parse_reviews(text)) is not None), None)

    website_node = card.select_one('a[data-value="Website"], a[aria-label*="Website"], a[data-item-id="authority"]')
    website = website_node.get("href") if isinstance(website_node, Tag) else None
    website = website if isinstance(website, str) else None
    tel_node = card.select_one('a[href^="tel:"]')
    phone_value = tel_node.get("href") if isinstance(tel_node, Tag) else None
    phone = phone_value.removeprefix("tel:") if isinstance(phone_value, str) else None
    phone = phone or _attribute_value(card, item_ids=("phone",), labels=("phone", "telephone"))
    address = _candidate_address(card)
    category = _candidate_category(card, name)

    latitude = longitude = None
    try:
        parsed = urlparse(source_url or page_url)
        coords = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", parsed.path + parsed.query)
        if coords:
            candidate = tuple(map(float, coords.groups()))
            if 6 <= candidate[0] <= 38 and 68 <= candidate[1] <= 98:
                latitude, longitude = candidate
    except (ValueError, TypeError):
        pass
    if latitude is None or longitude is None:
        latitude, longitude = parse_latlon(source_url)

    # Listing card layouts vary; retain source traceability and leave uncertain fields empty.
    return BusinessRecord.from_extracted({
        "discovery_source": "google_maps_browser",
        "business_name": name, "category": category, "address": address,
        "phone": phone, "website": website, "rating": rating,
        "review_count": review_count, "latitude": latitude, "longitude": longitude,
        "source_url": source_url, "source_ref": source_url or page_url,
    })


def parse_business_results(html: str, page_url: str = "https://www.google.com/maps") -> list[BusinessRecord]:
    """Parse visible Maps search cards; malformed or nameless cards are skipped."""
    if not isinstance(html, str) or not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('div[role="article"]')
    results: list[BusinessRecord] = []
    seen: set[str] = set()
    for card in cards:
        try:
            record = _parse_listing(card, page_url)
        except (ValueError, TypeError):
            continue
        if record is not None and record.lead_id not in seen:
            seen.add(record.lead_id)
            results.append(record)
    return results


def parse_business_detail(html: str, business: BusinessRecord) -> BusinessRecord:
    """Supplement a Maps result from its opened place panel, when fields are labeled."""
    if not isinstance(html, str) or not html.strip():
        return business
    soup = BeautifulSoup(html, "html.parser")
    updates: dict[str, str | None] = {}
    for field, item_ids, labels in (
        ("address", ("address",), ("address",)),
        ("phone", ("phone",), ("phone", "telephone")),
    ):
        value = _attribute_value(soup, item_ids=item_ids, labels=labels)
        if field == "phone":
            for node in soup.select('a[href^="tel:"], [data-item-id^="phone:tel:"]'):
                href = node.get("href")
                item_id = node.get("data-item-id")
                candidate = href.removeprefix("tel:") if isinstance(href, str) else None
                if not candidate and isinstance(item_id, str) and "tel:" in item_id:
                    candidate = item_id.split("tel:", 1)[1]
                if candidate and 7 <= len(re.sub(r"\D", "", candidate)) <= 15:
                    value = candidate
                    break
            if value:
                # Labels can describe a call action; retain only a plausible number.
                match = _PHONE_LIKE.search(value)
                value = match.group(0) if match else None
        if value and not getattr(business, field):
            updates[field] = value

    authority = soup.select_one('a[data-item-id="authority"], a[data-value="Website"], a[aria-label^="Website:"]')
    if authority is not None and not business.website:
        href = authority.get("href")
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            updates["website"] = href
    return business.model_copy(update=updates) if updates else business
