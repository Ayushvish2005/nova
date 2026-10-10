"""A person's decisions on what we escalated (feedback.json), kept on disk and applied to later reports.

Each stored item keeps its source (file, item id, who decided, when) so a report can say what it learned from
(`learned_at`). A fact applies only inside its scope: a `standing` fact needs every `applies_to` filter to match and the
date to fall in [valid_from, valid_to]; a `one_off` decision touches the one key it names and nothing else (GLOSSARY,
'feedback.json'). A fact counts only for reports whose as_of is on or after decided_at.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from facts import State


def ingest(path: str, st: State) -> int:
    """Store one feedback file. Idempotent: the same file twice stores once. -> number of items stored."""
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    sha = hashlib.sha256(raw).hexdigest()[:16]
    dest = st.root / "feedback" / f"{data.get('after_batch', 'x')}_{sha}.json"
    if dest.exists():
        return 0
    items = [it for it in data.get("items") or [] if isinstance(it, dict) and it.get("item_id") and it.get("decision")]
    dest.write_text(json.dumps({"file": Path(path).name, "sha": sha, "after_batch": data.get("after_batch"),
                                "decided_at": data.get("decided_at"), "decided_by": data.get("decided_by"),
                                "world": data.get("world"), "items": items}, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(items)


def _name(s) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


class Feedback:
    def __init__(self, st: State, as_of: str):
        self.items: list[dict] = []
        for f in st.all("feedback"):
            decided = (f.get("decided_at") or "")[:10]
            if decided and decided > as_of:
                continue                                   # not decided yet as of this report
            for it in f["items"]:
                self.items.append({**it, "_batch": f.get("after_batch"), "_decided_at": f.get("decided_at"),
                                   "_file": f.get("file")})

    # ---- scope ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def _in_dates(it: dict, on: str | None) -> bool:
        sc = it.get("scope") or {}
        on = (on or "")[:10]
        if not on:
            return True
        return (not sc.get("valid_from") or on >= sc["valid_from"]) and (not sc.get("valid_to") or on <= sc["valid_to"])

    def _standing(self, it: dict, **ctx) -> bool:
        sc = it.get("scope") or {}
        if sc.get("kind") != "standing":
            return False
        ap = sc.get("applies_to") or {}
        for k, v in ctx.items():
            if k in ("on",) or v is None:
                continue
            if k in ap and (_name(ap[k]) != _name(v) if k == "person" else str(ap[k]).upper() != str(v).upper()):
                return False
        return self._in_dates(it, ctx.get("on"))

    def _of(self, ftype: str, decision: tuple[str, ...] = ()) -> list[dict]:
        return [i for i in self.items if (i.get("fact") or {}).get("type") == ftype and (not decision or i["decision"] in decision)]

    # ---- what the engine asks -------------------------------------------------------------------------------------------
    def rate_accepted(self, cid: str, lane: str, pickup: str, sender: str, sent_on: str | None):
        """A rate notice the AP team confirmed with the carrier: treated as the rate for that contract and lane."""
        for it in self._of("rate_accepted", ("close", "confirm")):
            v = (it["fact"].get("value") or {})
            sub = it["fact"].get("subject") or {}
            if str(sub.get("contract_id", cid)).upper() != cid.upper() or str(sub.get("lane", lane)).upper() != lane.upper():
                continue
            if not self._standing(it, contract_id=cid, lane=lane, on=pickup):
                continue
            if v.get("sent_by") and _name(v["sent_by"]) != _name(sender):
                continue
            if v.get("sent_on") and sent_on and v["sent_on"] != sent_on:
                continue
            return it
        return None

    def precedence(self, cid: str, lane: str, pickup: str):
        for it in self._of("precedence", ("resolve_conflict",)):
            sub = it["fact"].get("subject") or {}
            if str(sub.get("contract_id", cid)).upper() == cid.upper() and str(sub.get("lane", lane)).upper() == lane.upper() \
                    and self._standing(it, contract_id=cid, lane=lane, on=pickup):
                return it
        return None

    def authority(self, cid: str, person: str, category: str, on: str | None):
        for it in self._of("sender_authority", ("confirm",)):
            sub, val = it["fact"].get("subject") or {}, it["fact"].get("value") or {}
            if _name(sub.get("person")) == _name(person) and category in (val.get("categories") or []) \
                    and self._standing(it, contract_id=cid, person=person, category=category, on=on):
                return it
        return None

    def closed(self, invoice: str, cls: str):
        """The key was closed or rejected: stop raising it (that key only)."""
        for it in self.items:
            fk = it.get("finding_key") or {}
            if fk.get("invoice_id", "").upper() == invoice.upper() and fk.get("class") == cls and it["decision"] in ("close", "reject"):
                return it
        return None

    def override(self, invoice: str, cls: str):
        for it in self.items:
            fk = it.get("finding_key") or {}
            if fk.get("invoice_id", "").upper() == invoice.upper() and fk.get("class") == cls and it["decision"] == "override_amount":
                return it
        return None

    def waiver_one_off(self, invoice: str):
        for it in self.items:
            fk = it.get("finding_key") or {}
            if fk.get("invoice_id", "").upper() == invoice.upper() and fk.get("class") == "waived_but_billed" \
                    and it["decision"] == "one_off_approval" and (it.get("fact") or {}).get("type") == "waiver_scope":
                return it
        return None


def learned(it: dict | None) -> dict | None:
    return {"feedback_item_id": it["item_id"], "batch": it.get("_batch") or ""} if it else None
