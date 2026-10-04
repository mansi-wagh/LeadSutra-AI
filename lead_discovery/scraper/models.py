from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BusinessRecord(BaseModel):
    """Verified, basic business listing data returned by discovery."""

    model_config = ConfigDict(extra="ignore")

    lead_id: str
    place_id: str | None = None
    primary_type: str | None = None
    discovery_source: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    source_attributions: list[dict[str, Any]] = Field(default_factory=list)
    business_name: str
    category: str | None = None
    sub_category: str | None = None
    description: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    rating: float | None = None
    review_count: int | None = None
    source_url: str | None = None
    source_ref: str | None = None

    @field_validator("business_name")
    @classmethod
    def name_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("business_name must not be empty")
        return value

    @field_validator("email")
    @classmethod
    def valid_email_only(cls, value: str | None) -> str | None:
        """Reject URLs and malformed values in the email slot."""
        if value is None:
            return None
        value = value.strip()
        if (not value or "://" in value or value.casefold().startswith("www.")
                or not re.fullmatch(r"[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+", value)):
            return None
        return value.casefold()

    @field_validator("rating")
    @classmethod
    def valid_rating(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 5:
            raise ValueError("rating must be between 0 and 5")
        return value

    @field_validator("review_count")
    @classmethod
    def valid_review_count(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("review_count cannot be negative")
        return value

    @classmethod
    def stable_id(cls, source_url: str | None, name: str, address: str | None, place_id: str | None = None) -> str:
        """Derive a repeatable ID from a listing URL, or name/address if absent."""
        identity = (f"places:{place_id}" if place_id else source_url) or json.dumps(
            [name.casefold().strip(), (address or "").casefold().strip()]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def from_extracted(cls, data: dict[str, Any]) -> "BusinessRecord":
        values = dict(data)
        values.setdefault("lead_id", cls.stable_id(values.get("source_url"), values.get("business_name", ""),
                                                    values.get("address"), values.get("place_id")))
        return cls.model_validate(values)
