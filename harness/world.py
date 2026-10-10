"""The world as of one date: every fact from documents received by then, indexed for the rules.

World(state, as_of) is rebuilt from disk on every `report`, so a report is a pure function of (state, as_of): the order
files arrived in cannot change an answer. Nothing here calls a model.
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

import docreader
from facts import State, invoice_id, received_by
from feedback import Feedback


def norm_label(s: str | None) -> str:
    """Compare charge labels without case, spaces or punctuation (OCR drops spaces)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def num(v) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[^\d.\-]", "", str(v)))
    except Exception:
        return None


class World:
    def __init__(self, st: State, as_of: str):
        self.st, self.as_of = st, as_of
        self.docs = {d["doc_id"]: d for d in st.all("docs")}
        self.facts = [f for f in st.all("facts") if received_by(f["received_at"], as_of)]
        self.contracts: dict[str, dict] = {}
        self.bills: dict[str, list[dict]] = defaultdict(list)        # invoice id -> every copy received, earliest first
        self.credits: list[dict] = []
        self.events: dict[str, list[dict]] = defaultdict(list)       # awb -> tracking / milestone events
        self.awbs: dict[str, dict] = {}
        self.dgds: dict[str, dict] = {}
        self.ap: dict[str, dict] = {}                                # invoice id -> row of the latest register
        self.assertions: list[dict] = []
        self.fb = Feedback(st, as_of)
        self._load()

    # ---- loading --------------------------------------------------------------------------------------------------------
    def _load(self) -> None:
        for f in sorted(self.facts, key=lambda f: (f["received_at"], f["doc_id"])):
            k, d = f["kind"], f["data"] or {}
            if k in ("contract", "amendment", "annex"):
                self._add_contract_doc(f, d)
            elif k == "billing":
                b = {**d, "doc_id": f["doc_id"], "received_at": f["received_at"], "id": invoice_id(d.get("number") or ""),
                     "via": f.get("via")}
                if not b["id"]:
                    continue
                (self.credits if d.get("doc_type") == "credit_note" else self.bills[b["id"]]).append(b)
            elif k == "tracking":
                for r in d.get("rows") or []:
                    self.events[r["awb"]].append({"event": r["event"].upper(), "date": r["date"], "doc_id": f["doc_id"],
                                                  "ref": r["ref"], "source": "tracking"})
            elif k == "awb":
                for key in [d.get("awb")] + [h.get("awb") for h in d.get("houses") or []]:
                    if key:
                        self.awbs[key] = {**d, "doc_id": f["doc_id"]}
            elif k == "dgd":
                if d.get("awb"):
                    self.dgds[d["awb"]] = {**d, "doc_id": f["doc_id"]}
            elif k == "ap_register":
                snap = d.get("as_of") or f["received_at"][:10]
                for r in d.get("rows") or []:
                    iid = invoice_id(r["invoice_no"])
                    if iid not in self.ap or self.ap[iid]["_snap"] <= snap:
                        self.ap[iid] = {**r, "_snap": snap, "doc_id": f["doc_id"]}
            elif k == "email":
                meta = self.docs.get(f["doc_id"], {})
                for a in d.get("assertions") or []:
                    self.assertions.append({**a, "doc_id": f["doc_id"], "from_addr": meta.get("from_addr", ""),
                                            "from_name": meta.get("from_name", ""), "received_at": f["received_at"]})
                    if a.get("type") == "milestone" and a.get("event") and a.get("event_date") and not a.get("in_quoted_text"):
                        for awb in a.get("awbs") or []:
                            self.events[awb].append({"event": a["event"].upper(), "date": a["event_date"],
                                                     "doc_id": f["doc_id"], "ref": (a.get("refs") or ["p1.1"])[0],
                                                     "source": "email", "from_addr": meta.get("from_addr", "")})

    def _add_contract_doc(self, f: dict, d: dict) -> None:
        cid = (d.get("contract_id") or "").strip().upper()
        if not cid:
            m = re.search(r"([A-Z]{2,4}(?:-[A-Z0-9]+){2,})", f["doc_id"].split("#")[-1])
            cid = m[1] if m else ""
        if d.get("role") == "amendment" and d.get("amendment_ref"):
            cid = d["amendment_ref"].split("/")[0].upper()
        if not cid:
            return
        c = self.contracts.setdefault(cid, {"id": cid, "msa": None, "amendments": [], "annexes": []})
        item = {"doc_id": f["doc_id"], "received_at": f["received_at"], "data": d}
        if d.get("role") == "amendment":
            c["amendments"].append(item)
        elif d.get("role") in ("annex_revision", "rate_schedule"):
            c["annexes"].append(item)
        elif c["msa"] is None or f["received_at"] > c["msa"]["received_at"]:
            c["msa"] = item

    # ---- contracts ------------------------------------------------------------------------------------------------------
    def contract(self, cid: str | None) -> dict | None:
        c = self.contracts.get((cid or "").strip().upper())
        return c if c and c["msa"] else None

    def people(self, cid: str) -> list[dict]:
        c = self.contract(cid)
        return (c["msa"]["data"].get("people") or []) if c else []

    def carrier_party(self, cid: str) -> str | None:
        """The Schedule 1 party code of the carrier: the one the contract id starts with (TBX-US-EXP-01 -> TBX-US)."""
        for p in {x.get("party") for x in self.people(cid)}:
            if p and cid.upper().startswith(p.upper() + "-"):
                return p
        return None

    def contact(self, cid: str, category: str, party: str | None = None) -> dict | None:
        party = party or self.carrier_party(cid)
        return next((p for p in self.people(cid) if category in (p.get("categories") or []) and p.get("party") == party), None)

    def has_authority(self, cid: str, addr: str, category: str, party: str | None = None, name: str | None = None,
                      on: str | None = None) -> dict | None:
        """The Schedule 1 person with this email address for this category on the carrier side. Failing that, a person
        a feedback decision confirmed for this contract and category (by name, for documents dated inside its scope)."""
        party = party or self.carrier_party(cid)
        a = (addr or "").lower()
        p = next((p for p in self.people(cid) if (p.get("email") or "").lower() == a and p.get("party") == party
                  and category in (p.get("categories") or [])), None)
        if p or not name:
            return p
        it = self.fb.authority(cid, name, category, on)
        return {"name": name, "ref": None, "fb": it, "categories": [category]} if it else None

    def terms(self, cid: str) -> dict:
        c = self.contract(cid)
        return (c["msa"]["data"].get("terms") or {}) if c else {}

    def code_for(self, cid: str, label: str | None) -> str | None:
        """Annex E: the charge code a printed label stands for."""
        c = self.contract(cid)
        want = norm_label(re.sub(r"^(?:the|a|an)\s+", "", re.sub(r"\(.*?\)", "", label or ""), flags=re.I))
        if not c or not want:
            return None
        for row in c["msa"]["data"].get("code_map") or []:
            names = [row.get("label")] + list(row.get("aliases") or [])
            if any(norm_label(n) == want for n in names):
                return row.get("code")
        return None

    def code_in_text(self, cid: str, text: str) -> str | None:
        """The charge code whose label or alias is named (longest first) anywhere in free text, e.g. a waiver sentence."""
        c = self.contract(cid)
        t = norm_label(text)
        best = None
        for row in (c["msa"]["data"].get("code_map") or []) if c else []:
            for n in [row.get("label")] + list(row.get("aliases") or []):
                nn = norm_label(n)
                if len(nn) >= 5 and nn in t and (best is None or len(nn) > best[0]):
                    best = (len(nn), row.get("code"))
        return best[1] if best else None

    def fee_row(self, cid: str, code: str) -> dict | None:
        c = self.contract(cid)
        return next((a for a in (c["msa"]["data"].get("accessorials") or []) if a.get("code") == code), None) if c else None

    def fuel_pct(self, cid: str, pickup: str) -> tuple[Decimal, dict] | None:
        c = self.contract(cid)
        if not c:
            return None
        row = next((r for r in c["msa"]["data"].get("fuel_rows") or [] if r.get("month") == pickup[:7]), None)
        return (num(row["pct"]), row) if row and num(row.get("pct")) is not None else None

    def fx(self, cid: str, invoice_date: str) -> tuple[Decimal, dict] | None:
        c = self.contract(cid)
        if not c:
            return None
        row = next((r for r in c["msa"]["data"].get("fx_rows") or [] if r.get("month") == invoice_date[:7]), None)
        return (num(row["inr_per_usd"]), row) if row and num(row.get("inr_per_usd")) else None

    # ---- shipments ------------------------------------------------------------------------------------------------------
    def pickup(self, awb: str, cid: str | None, line_date: str | None) -> tuple[str | None, str, dict | None]:
        """(date, source, evidence-ref) from the first of: tracking PICKED_UP, a carrier 'picked up' email, the AWB date,
        the date printed on the invoice line."""
        base = awb.split("-H")[0]
        evs = [e for k in {awb, base} for e in self.events.get(k, []) if e["event"] == "PICKED_UP"]
        for src in ("tracking", "email"):
            for e in sorted(evs, key=lambda e: e["date"]):
                if e["source"] != src:
                    continue
                if src == "email" and cid and not self.has_authority_any(cid, e.get("from_addr", "")):
                    continue                                  # a 'picked up' email must come from the carrier
                return e["date"], src, e
        a = self.awbs.get(awb) or self.awbs.get(base)
        if a and a.get("executed_date"):
            return a["executed_date"], "awb", {"doc_id": a["doc_id"], "ref": None}
        return (line_date, "invoice", None) if line_date else (None, "none", None)

    def has_authority_any(self, cid: str, addr: str) -> bool:
        party = self.carrier_party(cid)
        return any((p.get("email") or "").lower() == (addr or "").lower() and p.get("party") == party for p in self.people(cid))

    def chargeable_kg(self, awb: str) -> Decimal | None:
        a = self.awbs.get(awb)
        if not a:
            return None
        for h in a.get("houses") or []:
            if h.get("awb") == awb:
                return num(h.get("chargeable_weight_kg"))
        return num(a.get("chargeable_weight_kg"))

    def event_date(self, awb: str, event: str) -> tuple[str, dict] | None:
        """The date of a shipment event, looked up under the waybill as printed and under its master (house ids carry -H1)."""
        evs = sorted((e for k in {awb, awb.split("-H")[0]} for e in self.events.get(k, []) if e["event"] == event),
                     key=lambda e: (e["source"] != "tracking", e["date"]))
        return (evs[0]["date"], evs[0]) if evs else None

    # ---- evidence -------------------------------------------------------------------------------------------------------
    def ev(self, doc_id: str | None, ref: str | None, quote: str | None = None, page: int | None = None) -> dict | None:
        """An evidence entry {doc, page, quote}: the exact text of a stored line, or `quote` if it is on that page."""
        d = self.docs.get(doc_id or "")
        if not d:
            return None
        if ref:
            m = re.fullmatch(r"p(\d+)\.(\d+)", ref)
            if not m:
                return None
            page, line = int(m[1]), int(m[2])
            lines = d["pages"][page - 1].split("\n") if 1 <= page <= len(d["pages"]) else []
            if not 1 <= line <= len(lines):
                return None
            text = lines[line - 1].strip()
        else:
            text = quote
        if not text or len(re.sub(r"\s", "", text)) < 12:
            text = (text or "")
        return {"doc": doc_id, "page": page or 1, "quote": text}

    def find_line(self, doc_id: str, pattern: str) -> dict | None:
        """The first line of a document matching a regex, as an evidence entry (used to cite a clause)."""
        d = self.docs.get(doc_id)
        if not d:
            return None
        for pi, page in enumerate(d["pages"], 1):
            for li, ln in enumerate(page.split("\n"), 1):
                if re.search(pattern, ln, re.I):
                    # a clause wraps over lines: cite the matching line and the next, as one quote
                    nxt = page.split("\n")[li] if li < len(page.split("\n")) else ""
                    q = ln.strip()
                    if not re.search(r"[.;]\s*$", q) and nxt.strip() and not re.match(r"^\d+(\.\d+)*\s", nxt.strip()):
                        q = q + " " + nxt.strip()
                    return {"doc": doc_id, "page": pi, "quote": q}
        return None
