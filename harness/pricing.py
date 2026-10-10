"""Contract re-rating and tariff arbitration engine.

Reconstructs expected freight line charges based on pickup timestamp, applicable MSA
tariffs, executed amendments, authorized rate notices, and Annex F exchange schedules.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from facts import cents, to_date
from world import World, norm_label, num

D = Decimal
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december"]


def awb_base(awb: str) -> str:
    return awb.split("-H")[0]


def _band(rows: list[dict], weight: D) -> dict | None:
    cand = [r for r in rows if r.get("kind") == "rate" and num(r.get("band_min")) is not None and num(r["band_min"]) <= weight]
    return max(cand, key=lambda r: num(r["band_min"])) if cand else None


def _col(msa: dict, pickup: str) -> int | None:
    """Which rate column (0-based) the pickup date falls in; after the last period the last column continues."""
    periods = msa.get("periods") or []
    cols = msa.get("rate_columns") or []
    if not periods:
        return 0
    inside = next((p for p in periods if p["from"] <= pickup <= p["to"]), None)
    if inside is None:
        if pickup > max(p["to"] for p in periods):
            inside = max(periods, key=lambda p: p["to"])
        else:
            return None
    names = [norm_label(c) for c in cols]
    return names.index(norm_label(inside["name"])) if norm_label(inside["name"]) in names else periods.index(inside)


def period_name(msa: dict, pickup: str) -> str | None:
    """The rate period (P1, P2 ...) a pickup date falls in; the last one continues after the term (clause 3.1)."""
    periods = msa.get("periods") or []
    inside = next((p for p in periods if p["from"] <= pickup <= p["to"]), None)
    if inside is None and periods and pickup > max(p["to"] for p in periods):
        inside = max(periods, key=lambda p: p["to"])
    return inside["name"] if inside else None


def _val(row: dict | None, idx: int) -> D | None:
    return num(row.get(f"v{idx + 1}")) if row else None


def _source(kind, doc_id, rows, lane, weight, idx, eff, label):
    lane_rows = [r for r in rows if (r.get("lane") or "").upper() == lane]
    band = _band(lane_rows, weight)
    if not band or _val(band, idx) is None:
        return None
    mn = next((r for r in lane_rows if r.get("kind") == "minimum_charge"), None)
    return {"kind": kind, "doc_id": doc_id, "rate": _val(band, idx), "band_ref": band.get("ref"),
            "min": _val(mn, idx), "min_ref": mn.get("ref") if mn else None, "eff": eff, "label": label}


def rate_sources(w: World, cid: str, lane: str, weight: D, pickup: str) -> dict:
    """{'options': [source, ...] (1, or 2 when authorised sources conflict), 'missing': doc|None, 'unauth': source|None}"""
    c = w.contract(cid)
    msa = c["msa"]["data"]
    idx = _col(msa, pickup)
    out = {"options": [], "missing": None, "unauth": None, "conflict": False, "fb": []}
    base = _source("msa", c["msa"]["doc_id"], msa.get("rate_rows") or [], lane, weight, idx if idx is not None else 0,
                   (msa.get("terms") or {}).get("effective_date"), "contract Annex A") if idx is not None else None
    # amendments and the annex revisions they cite
    dated = []
    for am in c["amendments"]:
        a = am["data"]
        if a.get("unsigned_marker") or not a.get("effective_from") or a["effective_from"] > pickup:
            continue
        s = _source("amendment", am["doc_id"], a.get("rate_rows") or [], lane, weight, 0, a["effective_from"],
                    f"amendment {a.get('amendment_no') or ''}".strip())
        if s:
            dated.append(s)
        for cd in a.get("cited_documents") or []:
            if lane not in [x.upper() for x in cd.get("lanes") or []] or not cd.get("applies_from") or cd["applies_from"] > pickup:
                continue
            held = next((x for x in c["annexes"] if norm_label(x["data"].get("annex_name") or "") == norm_label(cd["name"])), None)
            if held is None:
                dated.append({"kind": "missing", "eff": cd["applies_from"], "name": cd["name"], "doc_id": am["doc_id"]})
            else:
                s = _source("annex_rev", held["doc_id"], held["data"].get("rate_rows") or [], lane, weight, 0,
                            cd["applies_from"], cd["name"])
                if s:
                    dated.append(s)
    top = max(dated, key=lambda s: s["eff"]) if dated else None
    if top and top["kind"] == "missing":
        out["missing"] = top
        return out
    amend = top
    # rate notices: emails with rate rows for this lane in force on the pickup date
    auth_notice = unauth_notice = None
    for a in w.assertions:
        if a.get("type") != "rate_notice" or a.get("in_quoted_text") or lane not in [x.upper() for x in a.get("lanes") or []]:
            continue
        if not a.get("effective_from") or a["effective_from"] > pickup or (a.get("effective_to") and pickup > a["effective_to"]):
            continue
        ref = (a.get("reference") or "")
        if "/" in ref and ref.split("/")[0].upper() != cid:
            continue
        rows = [{"lane": r["lane"].upper(), "kind": r.get("kind"), "band_min": r.get("band_min"), "v1": r.get("value"),
                 "ref": (a.get("refs") or [None])[0]} for r in a.get("rates") or []]
        s = _source("notice", a["doc_id"], rows, lane, weight, 0, a["effective_from"], "rate notice")
        if not s:
            continue
        s["rows"] = [(r["band_min"], r["v1"]) for r in rows]
        s["refs"] = a.get("refs") or []
        s["sender"] = a.get("from_addr")
        accepted = None if w.has_authority(cid, a.get("from_addr", ""), "rates") else             w.fb.rate_accepted(cid, lane, pickup, a.get("from_name", ""), a.get("received_at", "")[:10])
        if w.has_authority(cid, a.get("from_addr", ""), "rates") or accepted:
            s["fb"] = accepted
            if auth_notice is None or s["eff"] >= auth_notice["eff"]:
                auth_notice = s
        elif unauth_notice is None or s["eff"] >= unauth_notice["eff"]:
            unauth_notice = s
    out["unauth"] = unauth_notice
    cur = amend or base
    if auth_notice and auth_notice.get("fb"):
        out["fb"].append(auth_notice["fb"])
    if auth_notice:
        if amend and auth_notice["rate"] != amend["rate"] or (amend and (auth_notice["min"] or D(0)) != (amend["min"] or D(0))):
            out["options"], out["conflict"] = [amend, auth_notice], True
        elif amend:
            out["options"] = [amend]
        else:
            out["options"] = [auth_notice]
    elif cur:
        out["options"] = [cur]
    if out["conflict"]:                                   # a person decided which source rules this lane
        pf = w.fb.precedence(cid, lane, pickup)
        if pf:
            use = str((pf["fact"].get("value") or {}).get("use", "")).lower()
            out["options"] = [o for o in out["options"] if (o["kind"] == "notice") == ("notice" in use)] or out["options"]
            out["conflict"] = len(out["options"]) > 1
            out["fb"].append(pf)
    return out


# ---- one shipment ---------------------------------------------------------------------------------------------------------
def _months_in(text: str) -> set[int]:
    names = [m for m in MONTHS if m in (text or "").lower()]
    if len(names) == 2 and re.search(r"(?i)\bto\b|-", text or ""):
        a, b = MONTHS.index(names[0]) + 1, MONTHS.index(names[1]) + 1
        return set(range(a, b + 1)) if a <= b else set(range(a, 13)) | set(range(1, b + 1))
    return {MONTHS.index(n) + 1 for n in names}


def _fee(w: World, cid: str, code: str):
    r = w.fee_row(cid, code)
    return (num(r["fee"]), r) if r and num(r.get("fee")) is not None else (None, None)


def price_group(w: World, bill: dict, cid: str, awb: str, lines: list[dict]) -> dict:
    """Reprice every priced line of one shipment. -> {awb, status, options, pickup, lines:[{line, code, billed, correct[], ev[]}]}"""
    base = awb_base(awb)
    freight = next((l for l in lines if w.code_for(cid, l.get("description")) == "FRT"), None)
    line_date = next((l["date"] for l in lines if l.get("date")), None)
    pickup, psrc, pev = w.pickup(awb, cid, line_date)
    res = {"awb": awb, "status": "ok", "pickup": pickup, "pickup_source": psrc, "pickup_ev": pev, "lines": [],
           "options": 1, "missing": None, "unauth": None, "rate_labels": [], "fb": []}
    c = w.contract(cid)
    eff = (c["msa"]["data"].get("terms") or {}).get("effective_date") if c else None
    if not pickup or (eff and pickup < eff):
        res["status"] = "skip"
        return res
    opts = [None]
    if freight:
        lane = (freight.get("lane") or "").upper()
        weight = num(freight.get("qty")) or D(0)
        rs = rate_sources(w, cid, lane, weight, pickup)
        res["unauth"] = rs["unauth"]
        if rs["missing"]:
            res["status"], res["missing"] = "missing", rs["missing"]
            return res
        if not rs["options"]:
            res["status"] = "skip"
            return res
        opts = rs["options"]
        res["options"] = len(opts)
        res["status"] = "conflict" if rs["conflict"] else "ok"
        res["rate_labels"] = [o["label"] for o in opts]
        res["period"] = period_name(c["msa"]["data"], pickup)
        res["fb"] = rs["fb"]
    fuel = w.fuel_pct(cid, pickup)
    msa_doc = c["msa"]["doc_id"]
    clause = w.find_line(msa_doc, r"pricing date")
    dgd = w.dgds.get(base) or w.dgds.get(awb)
    items = (dgd or {}).get("items") or []
    dry_only = bool(items) and not any(i.get("is_dangerous_goods") for i in items)
    other_dg = any(i.get("is_dangerous_goods") for i in items)
    freight_correct: list[D] = []
    for o in opts:
        if freight and o:
            f = cents(o["rate"] * (num(freight.get("qty")) or D(0)))
            if o["min"] is not None and f < o["min"]:
                f = o["min"]
            freight_correct.append(f)
        else:
            freight_correct.append(D(0))
    for l in lines:
        code = w.code_for(cid, l.get("description"))
        billed = num(l.get("amount"))
        if billed is None or code is None:
            continue
        correct: list[D] | None = None
        ev: list[dict] = []
        qty = num(l.get("qty")) or D(0)
        if code == "FRT" and freight is l and opts[0]:
            correct = list(freight_correct)
            for o in opts:
                for ref in (o.get("band_ref"), o.get("min_ref")):
                    if ref:
                        ev.append(w.ev(o["doc_id"], ref))
        elif code == "FUEL" and fuel and freight and opts[0]:
            correct = [cents(fuel[0] / 100 * f) for f in freight_correct]
            ev.append(w.ev(msa_doc, fuel[1].get("ref")))
        elif code == "SEC":
            fee, row = _fee(w, cid, "SEC")
            if fee is not None:
                correct = [cents(fee * qty)] * len(opts)
                ev.append(w.ev(msa_doc, row.get("ref")))
        elif code in ("DG", "DRYICE") and dgd:
            fee, row = _fee(w, cid, code)
            if fee is not None:
                allowed = other_dg if code == "DG" else (dry_only and not other_dg)
                correct = [fee if allowed else D(0)] * len(opts)
                ev += [w.ev(msa_doc, row.get("ref")), w.ev(dgd["doc_id"], items[0].get("ref"))]
        elif code == "STOR":
            fee, row = _fee(w, cid, "STOR")
            arr, rel = w.event_date(awb, "ARRIVED_FACILITY"), (w.event_date(awb, "DELIVERED") or w.event_date(awb, "COLLECTED_BY_CONSIGNEE"))
            free = (c["msa"]["data"].get("terms") or {}).get("storage_free_days")
            if fee is not None and arr and rel and free is not None:
                days = max(0, (to_date(rel[0]) - to_date(arr[0])).days - int(num(free)))
                correct = [cents(fee * days)] * len(opts)
                ev += [w.ev(msa_doc, row.get("ref")), w.ev(arr[1]["doc_id"], arr[1]["ref"]), w.ev(rel[1]["doc_id"], rel[1]["ref"])]
        elif code == "DLV":
            fee, row = _fee(w, cid, "DLV")
            delivered = w.event_date(awb, "DELIVERED")
            collected = w.event_date(awb, "COLLECTED_BY_CONSIGNEE")
            if fee is not None and (delivered or collected):
                correct = [fee if delivered else D(0)] * len(opts)
                e = (delivered or collected)[1]
                ev += [w.ev(msa_doc, row.get("ref")), w.ev(e["doc_id"], e["ref"])]
        elif code == "CUSTOMS":
            fee, row = _fee(w, cid, "CUSTOMS")
            hold = next((a for a in w.assertions if a.get("type") == "customs_hold" and (awb in (a.get("awbs") or []) or base in (a.get("awbs") or []))
                         and not a.get("in_quoted_text")), None)
            if fee is not None and hold:
                carrier_fault = bool(re.search(r"(?i)carrier", hold.get("reason") or ""))
                correct = [D(0) if carrier_fault else fee] * len(opts)
                ev += [w.ev(msa_doc, row.get("ref")), w.ev(hold["doc_id"], (hold.get("refs") or [None])[-1])]
        elif code == "PEAK":
            fee, row = _fee(w, cid, "PEAK")
            if fee is not None:
                months = _months_in((row.get("condition") or "") + " " + (row.get("basis") or ""))
                allowed = (not months) or int(pickup[5:7]) in months
                correct = [cents(fee * qty) if allowed else D(0)] * len(opts)
                ev.append(w.ev(msa_doc, row.get("ref")))
        elif code in ("ACCT", "SPROG", "PPROG"):
            fee, row = _fee(w, cid, code)
            if fee is not None:
                correct = [cents(fee * qty)] * len(opts)
                ev.append(w.ev(msa_doc, row.get("ref")))
        if correct is not None:
            res["lines"].append({"line": l, "code": code, "billed": billed, "correct": correct,
                                 "ev": [e for e in ev if e], "clause": clause})
    return res


def reprice(w: World, bill: dict) -> dict:
    """Group the bill's lines by shipment and reprice each. Statement-style lines without a waybill are priced alone."""
    cid = (bill.get("contract_ref") or "").upper()
    out = {"bill": bill, "cid": cid, "groups": [], "skipped": False}
    if not w.contract(cid):
        out["skipped"] = True
        return out
    by: dict[str, list[dict]] = {}
    for l in bill.get("lines") or []:
        if l.get("awb"):
            by.setdefault(l["awb"], []).append(l)
    for awb, lines in by.items():
        out["groups"].append(price_group(w, bill, cid, awb, lines))
    loose = [l for l in bill.get("lines") or [] if not l.get("awb") and not l.get("late_fee")]
    if loose:
        g = {"awb": None, "status": "ok", "pickup": bill.get("issue_date"), "pickup_source": "invoice", "pickup_ev": None,
             "lines": [], "options": 1, "missing": None, "unauth": None, "rate_labels": []}
        c = w.contract(cid)
        for l in loose:
            code = w.code_for(cid, l.get("description"))
            fee, row = _fee(w, cid, code) if code in ("ACCT", "SPROG", "PPROG") else (None, None)
            if fee is not None and num(l.get("amount")) is not None:
                g["lines"].append({"line": l, "code": code, "billed": num(l["amount"]),
                                   "correct": [cents(fee * (num(l.get("qty")) or D(0)))],
                                   "ev": [w.ev(c["msa"]["doc_id"], row.get("ref"))], "clause": None})
        if g["lines"]:
            out["groups"].append(g)
    return out
