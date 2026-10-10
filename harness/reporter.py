"""Audit report synthesizer and autonomous execution policy evaluator."""
from __future__ import annotations

from decimal import Decimal

from billing_checks import detect_duplicates, detect_late_fees_not_owed, detect_waived
from detectors import detect_overcharge
from portfolio import detect_cashback, detect_expiry, detect_late_fee_risk
from facts import State, end_of_day_utc, usd_str
from feedback import learned
from world import World

D = Decimal
THRESHOLD = D("5000.00")
# order matters: later detectors read earlier findings (late fees exclude amounts already disputed)
DETECTORS = [detect_overcharge, detect_duplicates, detect_waived, detect_late_fees_not_owed, detect_expiry,
             detect_late_fee_risk, detect_cashback]
FINDING_CLASSES = ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed")
OUTBOUND_AMOUNT_CLASSES = FINDING_CLASSES + ("cashback",)


def confidence(raw: dict) -> float:
    f = raw.get("features") or {}
    c = 0.95
    if raw["verdict"] in ("conflict", "cannot_determine"):
        c = 0.88
    c -= {"tracking": 0.0, "email": 0.0, "awb": 0.05, "invoice": 0.10, "none": 0.15}.get(f.get("pickup_source", "tracking"), 0)
    c -= 0.15 * bool(f.get("flags")) + 0.05 * bool(f.get("weak")) + 0.05 * bool(f.get("ocr"))
    return round(max(0.05, min(0.99, c)), 2)


def _unattended_ok(w: World, raw: dict, open_conflicts: set) -> bool:
    """The five checks of the unattended-action test (GLOSSARY)."""
    f = raw.get("features") or {}
    if raw["verdict"] != "finding":
        return False
    if f.get("pickup_source") not in (None, "tracking", "email"):          # 1 evidence complete: a record dates the pickup
        return False
    if any((raw.get("cid"), lane) in open_conflicts for lane in raw.get("lanes") or []):      # 2 no open conflict
        return False
    if f.get("flags"):                                                     # 5 clean extraction (a notice we did not rely on is no weakness)
        return False
    return bool(raw.get("cid") and w.contact(raw["cid"], "billing_disputes"))


def action_for(w: World, raw: dict, mode: str, open_conflicts: set, invoice_total: D) -> str:
    v, cls = raw["verdict"], raw["class"]
    if v == "no_finding":
        return "none"
    if v == "conflict":
        return "escalate"
    if v == "cannot_determine":
        return "execute:request_document" if mode == "autonomous" and raw.get("missing_doc", {}).get("holder") else \
            "draft:request_document"
    if cls == "expiry":
        return "internal:alert"
    if cls == "late_fee_risk":
        return "internal:payment_reminder"
    if cls == "cashback" and raw.get("track_only"):
        return "internal:track_credit"
    kind = "claim_credit" if cls == "cashback" else "dispute"
    if invoice_total >= THRESHOLD:
        return "human_review"
    if mode == "autonomous" and _unattended_ok(w, raw, open_conflicts):
        return f"execute:{kind}"
    return "human_review" if mode == "autonomous" else f"draft:{kind}"


def to_item(raw: dict, action: str) -> dict:
    cls = raw["class"]
    key = raw["key"]
    iid = key.get("invoice_id")
    if iid:
        ident = f"{cls}:{iid}"
    elif cls == "cashback":
        ident = f"cashback:{key['contract_id']}/{key['period']}/{key['credit_type']}"
    else:
        ident = f"{cls}:{key.get('contract_id')}"
    it = {"id": ident, "class": cls, **{k: v for k, v in key.items()}, "verdict": raw["verdict"],
          "amount_usd": usd_str(raw["amount"]) if raw.get("amount") is not None else None,
          "amount_range": [usd_str(raw["range"][0]), usd_str(raw["range"][1])] if raw.get("range") else None,
          "deadline": raw.get("deadline"), "action": action, "confidence": confidence(raw),
          "evidence": raw.get("evidence") or [], "missing_doc": raw.get("missing_doc"),
          "supersedes": raw.get("supersedes", []), "learned_at": learned((raw.get("learned") or [None])[0])}
    if raw.get("retro"):
        it["retro"] = True
    return it


def _apply_feedback(w: World, found: list[dict]) -> list[dict]:
    """Keys a person closed or rejected stop appearing; an overridden amount replaces ours (each with learned_at)."""
    out = []
    for r in found:
        inv = r["key"].get("invoice_id")
        if inv and w.fb.closed(inv, r["class"]):
            continue
        ov = w.fb.override(inv, r["class"]) if inv else None
        if ov and r["verdict"] == "finding":
            amt = (ov["fact"].get("value") or {}).get("amount_usd")
            if amt is not None:
                r = {**r, "amount": D(str(amt)), "learned": [ov]}
        out.append(r)
    return out


def _instrument(w: World, doc_id: str) -> bool:
    """An amendment, annex revision or rate notice: a document that carries effective dates and can be backdated."""
    return any(f["doc_id"] == doc_id and f["kind"] in ("amendment", "annex") for f in w.facts) or         any(a["doc_id"] == doc_id and a.get("type") == "rate_notice" for a in w.assertions)


def apply_ledger(st: State, w: World, as_of: str, mode: str, items: list[dict], raws: list[dict]) -> None:
    """Compare with what the last earlier report said. A key whose verdict or amount moved lists its old id in
    `supersedes`; a new key on an invoice we already held, decided by a document that arrived since, is `retro`.
    Then record this report so the next one can compare. Reading never changes an answer: only the markers are added."""
    led = st.get_file("reports.json", {"entries": []})
    earlier = [e for e in led["entries"] if e["as_of"] < as_of]
    prev = max(earlier, key=lambda e: e["as_of"]) if earlier else None
    by_id = {r_id: r for r_id, r in ((f"{r['class']}:{r['key'].get('invoice_id')}", r) for r in raws)}
    for it in items:
        old = prev["items"].get(it["id"]) if prev else None
        sig = {k: it.get(k) for k in ("verdict", "amount_usd", "amount_range", "deadline")}
        if old and old != sig:
            it["supersedes"] = [it["id"]]
        elif prev and not old:
            raw = by_id.get(it["id"])
            cutoff = end_of_day_utc(prev["as_of"])
            if raw and raw.get("bill") and raw["bill"]["received_at"] < cutoff and any(
                    e["doc"] in w.docs and w.docs[e["doc"].split("#")[0]]["received_at"] >= cutoff and _instrument(w, e["doc"])
                    for e in it["evidence"]):
                it["retro"] = True
    led["entries"] = [e for e in led["entries"] if not (e["as_of"] == as_of and e["mode"] == mode)] + [
        {"as_of": as_of, "mode": mode, "items": {i["id"]: {k: i.get(k) for k in ("verdict", "amount_usd", "amount_range", "deadline")}
                                                 for i in items}}]
    st.put_file("reports.json", led)


def build_report(st: State, as_of: str, mode: str) -> dict:
    w = World(st, as_of)
    raws: list[dict] = []
    for det in DETECTORS:
        raws += _apply_feedback(w, det(w, raws))
    open_conflicts = {(r.get("cid"), lane) for r in raws if r["verdict"] == "conflict" for lane in r.get("lanes") or []}
    # human_review: every outbound finding on one invoice (or cashback key) is summed against the threshold
    totals: dict = {}
    for r in raws:
        if r["verdict"] == "finding" and r["class"] in OUTBOUND_AMOUNT_CLASSES and r.get("amount") is not None:
            k = r["key"].get("invoice_id") or tuple(sorted(r["key"].items()))
            totals[k] = totals.get(k, D(0)) + r["amount"]
    items = []
    for r in raws:
        k = r["key"].get("invoice_id") or tuple(sorted(r["key"].items()))
        items.append(to_item(r, action_for(w, r, mode, open_conflicts, totals.get(k, D(0)))))
    apply_ledger(st, w, as_of, mode, items, raws)
    rep = {"schema_version": "1.0", "as_of": as_of, "mode": mode,
           "findings": [i for i in items if i["class"] in FINDING_CLASSES],
           "portfolio": {"cashback": [i for i in items if i["class"] == "cashback"],
                         "expiry": [i for i in items if i["class"] == "expiry"],
                         "late_fee_risk": [i for i in items if i["class"] == "late_fee_risk"]}}
    return rep
