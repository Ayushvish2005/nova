"""Unambiguous printed scalar values shared by local document readers."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

NUMBER = r"-?\d[\d,]*(?:\.\d+)?"
ISO_DATE = r"\d{4}-\d{2}-\d{2}"


def observed(raw: str) -> dict:
    """Accept explicit ISO dates only; do not resolve locale-dependent dates."""
    date.fromisoformat(raw)
    return {"raw": raw, "value": raw}


def number(raw: str) -> Decimal:
    if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", raw):
        raise ValueError("ambiguous numeric punctuation")
    value = Decimal(raw.replace(",", ""))
    if not value.is_finite():
        raise ValueError("non-finite number")
    return value


def heading(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def slash_date(raw: str, all_dates: list[str], *, day_first: bool | None = None) -> dict:
    """Resolve a slash convention only when another date in the document proves it."""
    values = [tuple(map(int, value.split("/"))) for value in all_dates]
    if day_first is None:
        day_first_seen = any(first > 12 and second <= 12 for first, second, _ in values)
        month_first_seen = any(second > 12 and first <= 12 for first, second, _ in values)
        if day_first_seen == month_first_seen:
            raise ValueError("ambiguous or inconsistent slash-date convention")
        day_first = day_first_seen
    first, second, year = map(int, raw.split("/"))
    parsed = date(year, second, first) if day_first else date(year, first, second)
    return {"raw": raw, "value": parsed.isoformat()}
