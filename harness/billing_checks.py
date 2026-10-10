"""Invoice audit detectors: duplicate invoices, waived charges, and unowed late fees."""
from __future__ import annotations

import math
import re
from decimal import Decimal

from detectors import _dedupe, holder_of, primary_bills, to_usd
from facts import cents, invoice_id, to_date
from world import World, num

D = Decimal
BENIGN_MARKERS = ("COPY", "FOR YOUR RECORDS", "DUPLICATE")      # a REMINDER of a paid invoice still asks for payment


def _ev_line(w: World, bill: dict, ref: str | None):
    return w.ev(bill["doc_id"], ref) if ref else None


def _priced_lines(w: World, bill: dict):
    """(line, code) for lines that stand for a charge: Annex E code known."""
    cid = (bill.get("contract_ref") or "").upper()
    for l in bill.get("lines") or []:
        code = w.code_for(cid, l.get("description"))
        if code and num(l.get("amount")) is not None:
            yield l, code


def _when(line: dict, bill: dict) -> str:
    """The month a charge belongs to: a statement line's reference month, else its shipment date, else the invoice month."""
    ref = line.get("reference") or ""
    return ref if re.fullmatch(r"\d{4}-\d{2}", ref) else (line.get("date") or bill.get("issue_date") or "")[:7]


def _credits_for(w: World, iid: str) -> D:
    return sum((num(c.get("total")) or D(0) for c in w.credits if invoice_id(c.get("applies_to_invoice") or "") == iid), D(0))


# ---- duplicates -------------------------------------------------------------------------------------------------------
def detect_duplicates(w: World, prior: list[dict]) -> list[dict]:
    out = []
    bills = [b for _, b in primary_bills(w)]
    # (a) the same invoice number sent again after the first copy is paid or scheduled, asking for payment
    for iid, copies in w.bills.items():
        if len(copies) < 2 or copies[0].get("doc_type") not in ("invoice", "statement", "debit_note"):
            continue
        row = w.ap.get(iid)
        if not row or (row.get("status") or "").lower() not in ("paid", "scheduled"):
            continue
        for later in copies[1:]:
            if (later.get("status_marker") or "").upper() in BENIGN_MARKERS:
                continue
            parent = later["doc_id"].split("#")[0]
            ask = next((a for a in w.assertions if a["doc_id"] == parent and a.get("type") == "payment_request"), None)
            total = num(later.get("total")) or num(copies[0].get("total"))
            if total is None:
                continue
            usd, fxev, miss = to_usd(w, later, total)
            if miss:
                continue
            ev = [w.ev(row["doc_id"], row["ref"]), w.ev(ask["doc_id"], ask["refs"][0]) if ask and ask.get("refs") else None,
                  w.find_line(later["doc_id"], r"^\s*(total|balance due)")]
            out.append({"class": "duplicate", "key": {"invoice_id": iid}, "verdict": "finding", "amount": usd,
                        "native": cents(total), "evidence": _dedupe(ev + [fxev]), "cid": (later.get("contract_ref") or "").upper(),
                        "lanes": [], "features": {"pickup_source": None, "ocr": False}, "bill": later})
            break
    # (b) one shipment line billed on two invoice numbers: the later invoice carries the finding
    seen: dict = {}
    order = sorted(bills, key=lambda b: (b.get("issue_date") or "", b["received_at"], b["id"]))
    flagged: dict[str, dict] = {}
    for b in order:
        if b.get("doc_type") == "credit_note":
            continue
        cid = (b.get("contract_ref") or "").upper()
        for l, code in _priced_lines(w, b):
            key = (cid, (l.get("awb") or l.get("reference") or "").upper(), code, cents(num(l["amount"])))
            if not key[1]:
                continue
            if code in ("TAX", "DUTY", "LATE"):
                continue
            if key in seen and seen[key]["bill"]["id"] != b["id"]:
                first = seen[key]
                f = flagged.setdefault(b["id"], {"bill": b, "lines": [], "evs": [], "firsts": set()})
                f["lines"].append(l)
                f["firsts"].add(first["bill"]["id"])
                f["evs"] += [w.ev(first["bill"]["doc_id"], first["line"]["ref"]), w.ev(b["doc_id"], l["ref"])]
            else:
                seen.setdefault(key, {"bill": b, "line": l})
    for iid, f in flagged.items():
        b = f["bill"]
        overlap = sum((num(l["amount"]) for l in f["lines"]), D(0))
        credit = sum((_credits_for(w, i) for i in f["firsts"]), D(0))
        amount = overlap - credit
        if amount <= 0:
            continue
        usd, fxev, miss = to_usd(w, b, amount)
        if miss:
            continue
        cid = (b.get("contract_ref") or "").upper()
        evs = f["evs"] + [w.find_line(w.contract(cid)["msa"]["doc_id"], r"^code ")] if w.contract(cid) else f["evs"]
        out.append({"class": "duplicate", "key": {"invoice_id": iid}, "verdict": "finding", "amount": usd,
                    "native": cents(amount), "evidence": _dedupe(evs + [fxev]), "cid": cid, "lanes": [],
                    "features": {"pickup_source": None, "ocr": bool(w.docs[b["doc_id"]].get("ocr"))}, "bill": b})
    return out


# ---- waived but billed -------------------------------------------------------------------------------------------------
def detect_waived(w: World, prior: list[dict]) -> list[dict]:
    out = []
    waivers = [a for a in w.assertions if a.get("type") == "waiver"]
    for iid, b in primary_bills(w):
        cid = (b.get("contract_ref") or "").upper()
        if not w.contract(cid):
            continue
        hit, evs, unresolved, learned = D(0), [], None, []
        one_off = w.fb.waiver_one_off(iid)
        for l, code in _priced_lines(w, b):
            awb = (l.get("awb") or "").split("-H")[0]
            for a in waivers:
                wc = w.code_for(cid, a.get("charge")) or w.code_in_text(cid, a.get("summary") or "")
                named = awb in [x.split("-H")[0] for x in a.get("awbs") or []] if awb and a.get("awbs") else \
                    bool(a.get("months")) and _when(l, b) in a["months"]
                if wc != code or not named:
                    continue
                person = w.has_authority(cid, a.get("from_addr", ""), "waivers", name=a.get("from_name"),
                                         on=(a.get("received_at") or "")[:10])
                if a.get("in_quoted_text"):                         # only seen quoted in someone's forward: ask for the original
                    author = a.get("original_author") or ""
                    holder = next((p for p in w.people(cid) if p.get("name") == author and "waivers" in (p.get("categories") or [])
                                   and p.get("party") == w.carrier_party(cid)), None) or w.contact(cid, "notices")
                    unresolved = unresolved or {"waiver": a, "line": l, "holder": holder}
                    continue
                if not person or a.get("condition"):
                    continue                                        # no authority, or conditional and not shown met: not a finding
                if person.get("fb"):
                    learned.append(person["fb"])
                hit += num(l["amount"])
                evs += [w.ev(a["doc_id"], a["refs"][0]) if a.get("refs") else None, w.ev(w.contract(cid)["msa"]["doc_id"], person["ref"]),
                        w.ev(b["doc_id"], l["ref"]), w.find_line(w.contract(cid)["msa"]["doc_id"], r"waiver authority|waived only")]
                break                                               # a line is waived once, however many emails say so
        if one_off and hit == 0:                                    # approved for the named months on this key only
            months = (one_off["fact"].get("value") or {}).get("months") or []
            who = (one_off["fact"].get("subject") or {}).get("person")
            for l, code in _priced_lines(w, b):
                when = _when(l, b)
                for a in waivers:
                    if re.sub(r"[^a-z]", "", (a.get("from_name") or "").lower()) != re.sub(r"[^a-z]", "", (who or "").lower())                             or (w.code_for(cid, a.get("charge")) or w.code_in_text(cid, a.get("summary") or "")) != code \
                            or when not in months or a.get("in_quoted_text"):
                        continue
                    if a.get("awbs") and (l.get("awb") or "").split("-H")[0] not in [x.split("-H")[0] for x in a["awbs"]]:
                        continue                                    # an approval adds the month; it never widens what the waiver names
                    if a.get("months") and when not in a["months"]:
                        continue                                    # nor does it move a waiver to a month the waiver did not name
                    hit += num(l["amount"])
                    learned.append(one_off)
                    evs += [w.ev(a["doc_id"], a["refs"][0]) if a.get("refs") else None, w.ev(b["doc_id"], l["ref"])]
                    break
        credit = _credits_for(w, iid)
        base = {"class": "waived_but_billed", "key": {"invoice_id": iid}, "cid": cid, "lanes": [], "bill": b, "learned": learned,
                "features": {"pickup_source": None, "ocr": bool(w.docs[b["doc_id"]].get("ocr"))}}
        if hit > 0 and hit - credit > 0:
            usd, fxev, miss = to_usd(w, b, hit - credit)
            if not miss:
                out.append({**base, "verdict": "finding", "amount": usd, "native": cents(hit - credit),
                            "evidence": _dedupe(evs + [fxev])})
        elif hit == 0 and unresolved:
            a, l = unresolved["waiver"], unresolved["line"]
            out.append({**base, "verdict": "cannot_determine", "amount": None,
                        "evidence": _dedupe([w.ev(a["doc_id"], a["refs"][0]) if a.get("refs") else None, w.ev(b["doc_id"], l["ref"])]),
                        "missing_doc": {"name": f"Original waiver email from {a.get('original_author') or 'the carrier'}",
                                        "holder": holder_of(unresolved["holder"])}})
    return out


# ---- late fees not owed ------------------------------------------------------------------------------------------------
def _due_date(w: World, cid: str, iid: str):
    """(due, evidence list, extended?) for an invoice: the contract's due date, replaced by an authorised extension."""
    bill = (w.bills.get(iid) or [None])[0]
    row = w.ap.get(iid)
    due = (row or {}).get("due_date") or (bill or {}).get("due_date")
    evs = [w.ev(row["doc_id"], row["ref"])] if row else []
    best = None
    for a in w.assertions:
        if a.get("type") != "due_date_extension" or a.get("in_quoted_text") or iid not in [invoice_id(x) for x in a.get("invoice_ids") or []]:
            continue
        if not w.has_authority(cid, a.get("from_addr", ""), "payment_terms"):
            continue
        if best is None or a["received_at"] >= best["received_at"]:
            best = a
    if best and best.get("new_due_date"):
        evs.append(w.ev(best["doc_id"], best["refs"][0]) if best.get("refs") else None)
        return best["new_due_date"], evs, True
    return due, evs, False


def detect_late_fees_not_owed(w: World, prior: list[dict]) -> list[dict]:
    out = []
    disputed: dict[str, D] = {}
    for p in prior:
        if p["verdict"] == "finding" and p["class"] in ("overcharge", "duplicate", "waived_but_billed") and p.get("native"):
            disputed[p["key"]["invoice_id"]] = disputed.get(p["key"]["invoice_id"], D(0)) + p["native"]
    for iid, b in primary_bills(w):
        cid = (b.get("contract_ref") or "").upper()
        t = w.terms(cid)
        r, grace = num(t.get("late_fee_pct_per_month")), t.get("late_grace_days")
        if not w.contract(cid) or r is None or grace is None:
            continue
        over, evs = D(0), []
        for l in b.get("lines") or []:
            lf = l.get("late_fee")
            if not lf or not lf.get("on_invoice"):
                continue
            y = invoice_id(lf["on_invoice"])
            row = w.ap.get(y)
            ybill = (w.bills.get(y) or [None])[0]
            paid = (row or {}).get("paid_date")
            due, due_evs, _ = _due_date(w, cid, y)
            principal = num((row or {}).get("amount")) or num((ybill or {}).get("total"))
            if not paid or not due or principal is None:
                continue                                              # cannot tell whether it was late: no finding
            principal -= disputed.get(y, D(0))                       # amounts under dispute never enter the base
            days_late = (to_date(paid) - to_date(due)).days
            month = int(num(lf.get("month")) or 1)
            billed = num(l["amount"])
            if days_late <= int(num(grace)):
                correct = D(0)
            else:
                months = math.ceil(days_late / 30)
                base, fees = principal, []
                for i in range(1, month + 1):
                    fee = cents(r / 100 * base)
                    fees.append(fee)
                    if t.get("late_compounding"):
                        base += fee
                correct = fees[-1] if month <= months else D(0)
            if billed > correct:
                over += billed - correct
                evs += [w.ev(b["doc_id"], l["ref"])] + due_evs + [w.find_line(w.contract(cid)["msa"]["doc_id"], r"^\d+(\.\d+)*\s+late fee"),
                                                                  w.find_line(w.contract(cid)["msa"]["doc_id"], r"extension of due date")]
        if over > 0:
            usd, fxev, miss = to_usd(w, b, over)
            if not miss:
                out.append({"class": "late_fee_not_owed", "key": {"invoice_id": iid}, "verdict": "finding", "amount": usd,
                            "native": cents(over), "evidence": _dedupe(evs + [fxev]), "cid": cid, "lanes": [],
                            "features": {"pickup_source": None, "ocr": bool(w.docs[b["doc_id"]].get("ocr"))}, "bill": b})
    return out
