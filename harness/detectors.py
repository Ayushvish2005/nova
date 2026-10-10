"""The six classes of problem, each a function World -> list of raw items.

A raw item is what was found, with its proof; reporter.py turns it into the report's action, confidence and mode
behaviour. Money is Decimal in USD unless a field says otherwise. Phase 3 has overcharge (with conflict and
cannot_determine); the other classes plug into DETECTORS.
"""
from __future__ import annotations

from decimal import Decimal

from facts import cents
from pricing import rate_sources, reprice
from world import World

D = Decimal


def _dedupe(evs: list[dict | None]) -> list[dict]:
    seen, out = set(), []
    for e in evs:
        key = (e["doc"], e["page"], " ".join(e["quote"].split())) if e and e.get("quote") else None
        if key and key not in seen:
            seen.add(key)
            out.append(e)
    return out


def primary_bills(w: World):
    """One bill per invoice number: the first copy received (later copies are resends, judged by the duplicate rule)."""
    for iid, copies in w.bills.items():
        b = copies[0]
        if b.get("doc_type") in ("invoice", "statement", "debit_note"):
            yield iid, b


def to_usd(w: World, bill: dict, amount: D):
    """-> (usd, evidence|None, missing_doc|None). INR converts once per finding at the Annex F rate of the invoice month."""
    if (bill.get("currency") or "USD").upper() == "USD":
        return cents(amount), None, None
    cid = (bill.get("contract_ref") or "").upper()
    fx = w.fx(cid, bill.get("issue_date") or "")
    if not fx:
        return None, None, {"name": "Annex F", "holder": holder_of(w.contact(cid, "treasury"))}
    return cents(amount / fx[0]), w.ev(w.contract(cid)["msa"]["doc_id"], fx[1].get("ref")), None


def holder_of(contact: dict | None) -> dict:
    if not contact:
        return {}
    return {"party": contact.get("party"), "person": contact.get("name"), "role": contact.get("title")}


def _weakest(groups) -> str:
    order = ["tracking", "email", "awb", "invoice", "none"]
    return max((g["pickup_source"] for g in groups if g["status"] != "skip"), key=order.index, default="none")


def _pickup_ev(w: World, g: dict):
    pe = g["pickup_ev"]
    if not pe or not pe.get("doc_id"):
        return None
    return w.ev(pe["doc_id"], pe["ref"]) if pe.get("ref") else w.find_line(pe["doc_id"], r"executed on")


def _conflict_sources(w: World, r: dict) -> list[dict | None]:
    """The documents that disagree: the amendment's row and the notice's row for the conflicting lane."""
    out = []
    for g in r["groups"]:
        freight = next((L for L in g["lines"] if L["code"] == "FRT"), None) if g["status"] == "conflict" else None
        if freight:
            rs = rate_sources(w, r["cid"], (freight["line"].get("lane") or "").upper(),
                              D(freight["line"]["qty"].replace(",", "")), g["pickup"])
            out += [w.ev(o["doc_id"], (o.get("refs") or [None])[-1] if o["kind"] == "notice" else o.get("band_ref"))
                    for o in rs["options"]]
    return out


# ---- overcharge (with conflict and cannot_determine) --------------------------------------------------------------------
def detect_overcharge(w: World, prior: list[dict] | None = None) -> list[dict]:
    out = []
    for iid, bill in primary_bills(w):
        r = reprice(w, bill)
        if r["skipped"] or not r["groups"]:
            continue
        cid = r["cid"]
        base = {"class": "overcharge", "key": {"invoice_id": iid}, "cid": cid, "bill": bill}
        miss = next((g for g in r["groups"] if g["status"] == "missing"), None)
        if miss:                                              # a cited annex we do not hold: ask its holder, decide nothing
            m = miss["missing"]
            line = next((l for l in bill["lines"] if l.get("awb") == miss["awb"]), None)
            out.append({**base, "verdict": "cannot_determine", "amount": None, "lanes": [],
                        "evidence": _dedupe([w.find_line(m["doc_id"], r"applies from|rev \d")] +
                                            ([w.ev(bill["doc_id"], line["ref"])] if line else [])),
                        "missing_doc": {"name": m["name"], "holder": holder_of(w.contact(cid, "rates"))},
                        "features": {"pickup_source": miss["pickup_source"]}})
            continue
        lo = hi = D(0)
        conflict, evs, lanes, weak, flags, learned = False, [], set(), [], [], []
        for g in r["groups"]:
            if g["status"] == "skip" or not g["lines"]:
                continue
            per_opt = [sum((max(D(0), L["billed"] - L["correct"][o]) for L in g["lines"]), D(0)) for o in range(g["options"])]
            glo, ghi = min(per_opt), max(per_opt)
            lo, hi = lo + glo, hi + ghi
            lanes.add(((g["lines"][0]["line"].get("lane") or "").upper(), g.get("period")))
            learned += g.get("fb") or []
            weak += ["unauthorised notice present"] * bool(g["unauth"])
            flags += ["arith" for L in g["lines"] if "arith" in (L["line"].get("flags") or [])]
            if ghi > 0:
                for L in g["lines"]:
                    if any(L["billed"] > c for c in L["correct"]):
                        evs += [w.ev(bill["doc_id"], L["line"]["ref"])] + L["ev"] + [L.get("clause")]
                evs.append(_pickup_ev(w, g))
            conflict = conflict or (g["status"] == "conflict" and glo != ghi)
        lanes = {lp for lp in lanes if lp[0]}
        feats = {"pickup_source": _weakest(r["groups"]), "flags": flags, "weak": weak, "ocr": bool(w.docs[bill["doc_id"]].get("ocr"))}
        base.update(lanes=sorted(lanes), features=feats, learned=learned)
        if conflict:
            evs += _conflict_sources(w, r) + [w.find_line(w.contract(cid)["msa"]["doc_id"], r"unranked|neither prevails")]
            lo_usd, _, _ = to_usd(w, bill, cents(lo))
            hi_usd, fxev, _ = to_usd(w, bill, cents(hi))
            out.append({**base, "verdict": "conflict", "amount": None, "range": (lo_usd, hi_usd),
                        "evidence": _dedupe(evs + [fxev])})
        elif hi > 0:
            usd, fxev, miss_fx = to_usd(w, bill, hi)
            if miss_fx:                                       # INR invoice and no Annex F row for its month
                out.append({**base, "verdict": "cannot_determine", "amount": None, "missing_doc": miss_fx,
                            "evidence": _dedupe(evs)})
            else:
                out.append({**base, "verdict": "finding", "amount": usd, "native": cents(hi), "evidence": _dedupe(evs + [fxev])})
    return out

