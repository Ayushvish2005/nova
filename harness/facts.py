"""State on disk and the small normalizers every other module shares.

State layout under --state-dir (plain JSON, inspectable):
    docs/<safe doc id>.json     one document: metadata + page text (no bytes)
    facts/<safe doc id>.json    what was extracted from it (Phase 2)
    llm_cache/                  model replies keyed by content hash (llm.py)
    feedback/<n>.json           feedback files as received (Phase 6)
    reports.json                ledger of what earlier reports said (Phase 5)
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


# ---- normalizers ------------------------------------------------------------------------------------------------------
def invoice_id(s: str) -> str:
    """As printed, in capitals, with spaces and the characters '#' and ':' taken out (GLOSSARY, finding key)."""
    return re.sub(r"[\s#:]", "", s).upper()


def money(s) -> Decimal:
    """'1,612.29', 'USD 12.65', 'INR45,219.11' -> Decimal. Raises ValueError when there is no number."""
    t = re.sub(r"[^\d.\-]", "", str(s))
    if t in ("", "-", ".", "-."):
        raise ValueError(f"no number in {s!r}")
    return Decimal(t)


def cents(d: Decimal) -> Decimal:
    """Half up to cents, ties away from zero for positives (and negatives, symmetrically). Nothing else rounds."""
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def usd_str(d: Decimal) -> str:
    return f"{cents(d):.2f}"


def to_date(s) -> date:
    return s if isinstance(s, date) and not isinstance(s, datetime) else date.fromisoformat(str(s)[:10])


def end_of_day_utc(d: str) -> str:
    """as_of 'YYYY-MM-DD' counts every document received through the end of that day (UTC)."""
    return (to_date(d) + timedelta(days=1)).isoformat() + "T00:00:00Z"


def received_by(received_at: str, as_of: str) -> bool:
    return received_at < end_of_day_utc(as_of)       # ISO strings of the same shape compare correctly


# ---- state dir --------------------------------------------------------------------------------------------------------
def safe(doc_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", doc_id)


class State:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        for sub in ("docs", "facts", "llm_cache", "feedback"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, doc_id: str) -> Path:
        return self.root / kind / f"{safe(doc_id)}.json"

    def has(self, kind: str, doc_id: str) -> bool:
        return self._path(kind, doc_id).is_file()

    def put(self, kind: str, doc_id: str, obj) -> None:
        p = self._path(kind, doc_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)                                # atomic: a killed ingest never leaves half a file

    def get(self, kind: str, doc_id: str):
        return json.loads(self._path(kind, doc_id).read_text(encoding="utf-8"))

    def all(self, kind: str) -> list:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.root / kind).glob("*.json"))]

    def put_file(self, name: str, obj) -> None:
        (self.root / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")

    def get_file(self, name: str, default=None):
        p = self.root / name
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default
