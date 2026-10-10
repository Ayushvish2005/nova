"""Portfolio-wide contract audits: volume rebates, annual credits, and expiry monitoring."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from billing_checks import _due_date
from detectors import _dedupe, primary_bills, to_usd
from facts import cents, to_date
from world import World, num

D = Decimal
UNPAID = ("open", "unpaid", "outstanding", "due", "pending", "overdue", "")


def _add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                                  # 29 Feb
        return d.replace(year=d.year + n, day=28)


# ---- expiry -----------------------------------------------------------------------------------------------------------
def detect_expiry(w: World, prior: list[dict]) -> list[dict]:
    out = []
    today = to_date(w.as_of)
    for cid, c in w.contracts.items():
        if not c["msa"]:
            continue
        t = (c["msa"]["data"].get("terms") or {})
        end, doc = t.get("term_end"), c["msa"]["doc_id"]
        ev = w.find_line(doc, r"initial term|ending on|expires on")
        ext = [a for a in c["amendments"] if (a["data"].get("terms") or {}).get("new_term_end") and not a["data"].get("unsigned_marker")]
        if ext:                                          # a signed extension moves the date
            last = max(ext, key=lambda a: a["data"]["effective_from"] or "")
            end, ev = last["data"]["terms"]["new_term_end"], w.find_line(last["doc_id"], r"extend|extension|new term")
        if not end:
            continue
        deadline, nev = to_date(end), None
        if t.get("auto_renews") and num(t.get("notice_days")):
            deadline = to_date(end) - timedelta(days=int(num(t["notice_days"])))
            nev = w.find_line(doc, r"notice")
        if 0 <= (deadline - today).days <= 60:
            out.append({"class": "expiry", "key": {"contract_id": cid}, "verdict": "finding", "amount": None,
                        "deadline": deadline.isoformat(), "evidence": _dedupe([ev, nev]), "cid": cid, "lanes": [],
                        "features": {"pickup_source": None}})
    return out


# ---- late-fee risk ----------------------------------------------------------------------------------------------------
def detect_late_fee_risk(w: World, prior: list[dict]) -> list[dict]:
    out = []
    today = to_date(w.as_of)
    disputed: dict[str, D] = {}
    for p in prior:
        if p["verdict"] == "finding" and p["class"] in ("overcharge", "duplicate", "waived_but_billed") and p.get("native"):
            disputed[p["key"]["invoice_id"]] = disputed.get(p["key"]["invoice_id"], D(0)) + p["native"]
    for iid, row in w.ap.items():
        bill = (w.bills.get(iid) or [None])[0]
        if not bill or (row.get("status") or "").lower() not in UNPAID or row.get("paid_date"):
            continue
        cid = (bill.get("contract_ref") or "").upper()
        r = num(w.terms(cid).get("late_fee_pct_per_month"))
        due, evs, _ = _due_date(w, cid, iid)
        if r is None or not due or not 0 < (to_date(due) - today).days <= 14:
            continue
        principal = (num(row.get("amount")) or num(bill.get("total")) or D(0)) - disputed.get(iid, D(0))
        usd, fxev, miss = to_usd(w, bill, cents(r / 100 * principal))
        if miss or principal <= 0:
            continue
        out.append({"class": "late_fee_risk", "key": {"invoice_id": iid}, "verdict": "finding", "amount": usd,
                    "deadline": due, "evidence": _dedupe(evs + [w.find_line(w.contract(cid)["msa"]["doc_id"], r"^\d+(\.\d+)*\s+late fee"), fxev]),
                    "cid": cid, "lanes": [], "features": {"pickup_source": None}})
    return out


# ---- cashback and credits ---------------------------------------------------------------------------------------------
def _qs_codes(w: World, cid: str) -> set[str]:
    """Charge codes that count as Qualifying Spend under the contract's definition."""
    c = w.contract(cid)["msa"]["data"]
    inc = " ".join((c.get("terms") or {}).get("qs_includes") or []).lower() or "base freight fuel security accessorials"
    codes = {"FRT"} if "freight" in inc or "base" in inc else set()
    if "fuel" in inc:
        codes.add("FUEL")
    if "security" in inc:
        codes.add("SEC")
    if "accessorial" in inc:
        codes |= {a["code"] for a in c.get("accessorials") or []}
    return codes


def _qs(w: World, cid: str, start: date, end: date):
    """Qualifying Spend in USD for invoices dated in [start, end], net of credit notes dated in it. -> (qs, evidence list)"""
    codes, total, evs = _qs_codes(w, cid), D(0), []
    for iid, b in primary_bills(w):
        if (b.get("contract_ref") or "").upper() != cid or not b.get("issue_date") or not start <= to_date(b["issue_date"]) <= end:
            continue
        native = sum((num(l["amount"]) for l in b["lines"] if num(l.get("amount")) is not None
                      and w.code_for(cid, l.get("description")) in codes), D(0))
        usd, _, miss = to_usd(w, b, native)
        if miss:
            continue
        total += usd
        evs.append(w.find_line(b["doc_id"], r"^\s*(total|balance due)"))
    for cn in w.credits:
        if (cn.get("contract_ref") or "").upper() == cid and cn.get("issue_date") and start <= to_date(cn["issue_date"]) <= end \
                and (cn.get("credit_type") or "") not in ("rebate", "improvement", "promised_credit", "margin_step_down"):
            usd, _, miss = to_usd(w, cn, num(cn.get("total")) or D(0))
            total -= usd or D(0)
            evs.append(w.find_line(cn["doc_id"], r"total"))
    return total, evs


def _issued(w: World, cid: str, period: str, kinds: tuple[str, ...]) -> D:
    return sum((num(c.get("total")) or D(0) for c in w.credits if (c.get("contract_ref") or "").upper() == cid
                and (c.get("credit_period") or "").upper() == period.upper() and (c.get("credit_type") or "") in kinds), D(0))


def detect_cashback(w: World, prior: list[dict]) -> list[dict]:
    out = []
    today = to_date(w.as_of)
    for cid, c in w.contracts.items():
        if not c["msa"]:
            continue
        t, doc = c["msa"]["data"].get("terms") or {}, c["msa"]["doc_id"]
        dates = [to_date(b["issue_date"]) for _, b in primary_bills(w) if (b.get("contract_ref") or "").upper() == cid and b.get("issue_date")]
        if not dates:
            continue
        # quarterly volume rebate
        tiers = sorted(((num(x["qs_min"]), num(x["pct"])) for x in t.get("rebate_tiers") or [] if num(x.get("qs_min")) is not None), key=lambda x: x[0])
        due_days = num(t.get("rebate_due_days_after_quarter_end")) or D(0)
        q = (min(dates).year, (min(dates).month - 1) // 3 + 1)
        while True:
            start = date(q[0], 3 * (q[1] - 1) + 1, 1)
            end = (date(q[0] + (q[1] == 4), (3 * q[1]) % 12 + 1, 1) - timedelta(days=1))
            if end >= today:
                break
            period = f"{q[0]}-Q{q[1]}"
            qs, evs = _qs(w, cid, start, end)
            tier = next((p for m, p in reversed(tiers) if qs >= m), None)
            if tier is not None:
                owed = cents(qs * tier / 100) - _issued(w, cid, period, ("rebate",))
                if owed > 0:
                    due = end + timedelta(days=int(due_days))
                    out.append({"class": "cashback", "key": {"contract_id": cid, "period": period, "credit_type": "rebate"},
                                "verdict": "finding", "amount": owed, "native": owed, "track_only": today <= due,
                                "evidence": _dedupe([w.find_line(doc, r"^\d+\.\d+\s+volume rebate"), w.find_line(doc, r"qualifying spend means")] + evs[:6]),
                                "cid": cid, "lanes": [], "features": {"pickup_source": None}})
            q = (q[0] + (q[1] == 4), q[1] % 4 + 1)
        # annual improvement credit, by contract year
        pct, eff = num(t.get("annual_credit_pct")), t.get("effective_date")
        if pct is not None and eff:
            n = 1
            while True:
                start, nxt = _add_years(to_date(eff), n - 1), _add_years(to_date(eff), n)
                end = nxt - timedelta(days=1)
                if end >= today:
                    break
                qs, evs = _qs(w, cid, start, end)
                owed = cents(qs * pct / 100) - _issued(w, cid, f"CY{n}", ("improvement", "promised_credit"))
                if owed > 0:
                    due = nxt + timedelta(days=int(num(t.get("annual_credit_due_days")) or 0))
                    out.append({"class": "cashback", "key": {"contract_id": cid, "period": f"CY{n}", "credit_type": "promised_credit"},
                                "verdict": "finding", "amount": owed, "native": owed, "track_only": today <= due,
                                "evidence": _dedupe([w.find_line(doc, r"^\d+\.\d+\s+annual improvement credit"), w.find_line(doc, r"qualifying spend means")] + evs[:6]),
                                "cid": cid, "lanes": [], "features": {"pickup_source": None}})
                n += 1
    return out
