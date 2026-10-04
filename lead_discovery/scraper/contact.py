from __future__ import annotations

import re
from collections import OrderedDict
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field

from .models import BusinessRecord
from .website import WebsiteCrawlResult

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+", re.I)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{5,}\d)(?:\s*(?:ext\.?|x)\s*\d{1,6})?(?!\w)", re.I)
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "email.com"}
SYSTEM_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"}
PLACEHOLDER_LOCAL_PARTS = {"name", "yourname", "user", "email", "youremail", "test", "example", "john.doe", "jane.doe"}
PERSON_RE = re.compile(
    r"^\s*(?:contact\s+person|primary\s+contact|contact|owner|founder|practice\s+manager|"
    r"for\s+(?:business\s+)?inquiries,?\s*contact)\s*:\s*"
    r"(?P<name>(?:Dr\.?\s+|Mr\.?\s+|Ms\.?\s+|Mrs\.?\s+)?[A-Z][A-Za-z'?-]+(?:\s+[A-Z][A-Za-z'?-]+){1,3})\s*$",
    re.I | re.M,
)


class ContactValue(BaseModel):
    value: str
    source_urls: list[str] = Field(min_length=1)


class ContactEmail(BaseModel):
    email: str
    source_urls: list[str] = Field(min_length=1)


class ContactPhone(BaseModel):
    value: str
    normalized: str
    source_urls: list[str] = Field(min_length=1)


class ContactExtraction(BaseModel):
    lead_id: str
    contact_person: ContactValue | None = None
    contact_emails: list[ContactEmail] = Field(default_factory=list)
    phone_numbers: list[ContactPhone] = Field(default_factory=list)
    contact_page_url: str | None = None
    email: str | None = None
    phone: str | None = None
    field_sources: dict[str, list[str]] = Field(default_factory=dict)


def _valid_email(raw: str) -> str | None:
    address = unquote(raw.strip().strip(".,;:<>[](){}").lower())
    if not EMAIL_RE.fullmatch(address):
        return None
    local, domain = address.rsplit("@", 1)
    if domain in PLACEHOLDER_DOMAINS or "." not in domain:
        return None
    if local.casefold() in SYSTEM_LOCAL_PARTS | PLACEHOLDER_LOCAL_PARTS:
        return None
    if any(token in domain for token in ("sentry.io", "wixpress.com", "wordpress.com", "shopifyemail.com")):
        return None
    return address


def _phone(raw: str) -> tuple[str, str] | None:
    original = re.sub(r"\s+", " ", raw).strip(" .,:;")
    digits = re.sub(r"\D", "", original)
    if not 7 <= len(digits) <= 15 or len(set(digits)) == 1:
        return None
    normalized = ("+" if original.startswith("+") else "") + digits
    return original, normalized


def normalize_phone(raw: str | None) -> str | None:
    """Return the same digits-only representation used in contacts.phone_numbers."""
    if not raw:
        return None
    found = _phone(raw)
    return found[1] if found else None


def _phone_key(normalized: str) -> str:
    digits = normalized.lstrip("+")
    # US country code 1 is often omitted on the same listed number.
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def extract_contacts(business: BusinessRecord, website: WebsiteCrawlResult) -> ContactExtraction:
    """Extract contact data only from the given business and previously collected pages."""
    if business.lead_id != website.lead_id:
        raise ValueError("business and website result lead_id values must match")
    emails: OrderedDict[str, list[str]] = OrderedDict()
    phones: OrderedDict[str, tuple[str, str, list[str]]] = OrderedDict()
    person: ContactValue | None = None
    for page in website.website_content:
        content = page.main_text
        contact_text = getattr(page, "contact_text", "") or ""
        path = urlparse(page.page_url).path.casefold()
        page_kind = page.page_type.casefold()
        person_source = page_kind in {"home", "about", "contact", "team"} or bool(
            re.search(r"/(?:about|our-story|contact(?:-us)?|contactus|get-in-touch|reach-out|team|people)(?:/|$)", path)
        )
        phone_source = page_kind in {"home", "about", "contact", "team", "services", "faq"} or bool(
            re.search(r"/(?:about|our-story|contact(?:-us)?|contactus|get-in-touch|reach-out|team|people)(?:/|$)", path)
        )
        if re.search(r"/(?:blog|news|articles?|posts?)(?:/|$)", path):
            content = ""
        contact_content = "\n".join(value for value in (content, contact_text) if value)
        for link in page.outgoing_links:
            parsed = urlparse(link.url)
            if parsed.scheme.lower() in {"mailto", "tel"}:
                contact_content += " " + unquote(parsed.path)
        for raw in EMAIL_RE.findall(contact_content):
            email = _valid_email(raw)
            if email:
                emails.setdefault(email, [])
                if page.page_url not in emails[email]:
                    emails[email].append(page.page_url)
        for link in page.outgoing_links:
            parsed = urlparse(link.url)
            if parsed.scheme.lower() == "tel":
                found = _phone(unquote(parsed.path))
                if found:
                    original, normalized = found
                    key = _phone_key(normalized)
                    phones.setdefault(key, (original, normalized, []))
                    if page.page_url not in phones[key][2]:
                        phones[key][2].append(page.page_url)
        phone_text = contact_text
        if phone_source:
            phone_text += "\n" + content
        for raw in PHONE_RE.findall(phone_text):
            found = _phone(raw)
            if found:
                original, normalized = found
                key = _phone_key(normalized)
                phones.setdefault(key, (original, normalized, []))
                if page.page_url not in phones[key][2]:
                    phones[key][2].append(page.page_url)
        if person is None and person_source:
            match = PERSON_RE.search(content)
            if match:
                person = ContactValue(value=re.sub(r"\s+", " ", match.group("name")).strip(), source_urls=[page.page_url])

    record_source = business.source_url or business.source_ref or business.website
    if business.email and record_source:
        value = _valid_email(business.email)
        if value:
            emails.setdefault(value, [])
            if record_source not in emails[value]:
                emails[value].append(record_source)
    if business.phone and record_source:
        found = _phone(business.phone)
        if found:
            original, normalized = found
            key = _phone_key(normalized)
            phones.setdefault(key, (original, normalized, []))
            if record_source not in phones[key][2]:
                phones[key][2].append(record_source)

    email_records = [ContactEmail(email=value, source_urls=sources) for value, sources in emails.items()]
    phone_records = [ContactPhone(value=value, normalized=normalized, source_urls=sources) for value, normalized, sources in phones.values()]
    result = ContactExtraction(
        lead_id=business.lead_id, contact_person=person, contact_emails=email_records,
        phone_numbers=phone_records, contact_page_url=website.contact_page_url,
        email=email_records[0].email if email_records else None,
        phone=phone_records[0].normalized if phone_records else None,
    )
    result.field_sources = {
        "contact_person": person.source_urls if person else [],
        "contact_emails": list(dict.fromkeys(url for item in email_records for url in item.source_urls)),
        "phone_numbers": list(dict.fromkeys(url for item in phone_records for url in item.source_urls)),
        "contact_page_url": [website.contact_page_url] if website.contact_page_url else [],
    }
    return result
