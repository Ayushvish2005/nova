"""Rule-based parsing engine for structured billing and contract documents.

Performs deterministic table extraction, column order resolution, and mathematical
closure validation (qty x rate = amount). Unresolved or ambiguous records return None,
allowing optional delegation to secondary fallback readers.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from facts import cents

MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\s*[A-Z][a-z]{2}\s*\d{4}"
DATE_RE = re.compile(DATE)
AWB_RE = re.compile(r"\b[A-Z]{2}\d{8}(?:-H\d+)?\b")
LANE_RE = re.compile(r"\b[A-Z]{2,4}\d-[A-Z]{2,4}\d\b")
ID_RE = re.compile(r"\b[A-Z]{2,4}(?:[-/][A-Z0-9]+)+\d[A-Z0-9/-]*\b")        # invoice / contract style ids
MONEY_END = re.compile(r"(-?[\d,]+\.\d{2})\s*$")


def to_iso(s: str | None) -> str | None:
    """ISO as is; dd/mm/yyyy day first; '07 Sep 2035'."""
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    m = re.fullmatch(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\s*(\d{4})", s)
    if m and m[2].lower() in MON:
        return f"{m[3]}-{MON[m[2].lower()]:02d}-{int(m[1]):02d}"
    return None


def dec(s) -> Decimal | None:
    try:
        return Decimal(str(s).replace(",", "").rstrip("%"))
    except (InvalidOperation, ValueError):
        return None


def clean_num(s: str) -> str:
    return s.replace(",", "")


def lines_of(doc: dict):
    """(page, line_no, text) for every non-empty line, numbered as extraction.numbered() shows them."""
    for pi, text in enumerate(doc["pages"], 1):
        for li, ln in enumerate(text.split("\n"), 1):
            if ln.strip():
                yield pi, li, ln


# ---- quoted text ------------------------------------------------------------------------------------------------------
_QUOTE_START = re.compile(r"^(>|-{2,}\s*(original|forwarded)|begin forwarded|on .{5,80} wrote:|from:.*sent:)", re.I)


def quoted_lines(page: str) -> set[int]:
    """1-based line numbers inside quoted or forwarded text."""
    out, inside = set(), False
    for i, ln in enumerate(page.split("\n"), 1):
        s = ln.strip()
        if i > 6 and _QUOTE_START.match(s):
            inside = True
        if inside or s.startswith(">"):
            out.add(i)
    return out


# ============================================================================================================ billing ====
def _L(*phrases: str) -> str:
    """Alternation of label phrases that tolerates missing spaces (OCR prints 'Invoicedate', 'Payableby')."""
    return "(?:" + "|".join(r"\s*".join(re.escape(c) for c in p.replace(" ", "")) for p in phrases) + ")"


_TITLE = re.compile(r"(?i)\b(" + _L("invoice", "freight bill no.", "charges advice", "statement of account", "credit note",
                                    "debit note", "bill no.") + r")\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)\b")
_ISSUE = re.compile(r"(?i)\b" + _L("invoice date", "issued on", "document date", "statement date", "credit date",
                                  "date of issue", "issue date") + rf"\s*({DATE})")
_DUE = re.compile(r"(?i)\b" + _L("due date", "settle by", "payable by", "payment due", "due on", "pay by") + rf"\s*({DATE})")
_CREF = re.compile(r"(?i)\b" + _L("contract reference", "service agreement", "contract", "agreement") +
                   r"\s*([A-Z]{2,4}(?:-[A-Z0-9]+){2,})")
_LATE = re.compile(r"(?i)late (?:payment )?fee|overdue interest")
_LATE_DET = re.compile(r"(?i)late (?:payment )?fee on (\S+?):?\s+(?:[A-Z]{3}\s*)?([\d,]+\.?\d*) overdue,? month (\d+) @ ([\d.]+)%")
_ROLE = [("amount", r"amount|net\b"), ("rate", r"unit price|rate|price"), ("qty", r"qty|quantity|basis|units|weight")]


def _col_order(header: str) -> list[str] | None:
    """The order of the numeric columns (qty / rate / amount) as the header lists them."""
    h, hits, used = header.lower(), [], []
    for role, pat in _ROLE:
        for m in re.finditer(pat, h):
            if not any(m.start() < e and s < m.end() for s, e in used):
                hits.append((m.start(), role))
                used.append((m.start(), m.end()))
                break
    hits.sort()
    order = [r for _, r in hits]
    return order if set(order) == {"qty", "rate", "amount"} else None


def _is_header(line: str) -> bool:
    l = line.lower()
    words = sum(bool(re.search(w, l)) for w in (r"awb|waybill|shipment", r"description|charge", r"lane|route",
                                               r"qty|basis|units|quantity", r"rate|price", r"amount|net\b"))
    return words >= 4 and _col_order(line) is not None


def _tokens_numbers(s: str):
    """number tokens left to right: (value_str, is_pct, unit or None)."""
    out = []
    for m in re.finditer(r"(?<![\w.])(-?\d[\d,]*\.?\d*)(%)?(?:\s*(kg|shpt|days?|months?|pcs|fee|pieces)\b)?", s):
        out.append((clean_num(m[1]), bool(m[2]), (m[3] or "").lower() or None))
    return out


def _parse_pdf_line(page: int, li: int, line: str, order: list[str], has_no: bool, has_sac: bool):
    s = line.strip()
    if has_no:
        s = re.sub(r"^\d+\s+", "", s)
    reference = None
    pm = re.search(r"\(([^)]*)\)", s)
    if pm:
        reference = pm[1]
        s = s.replace(pm[0], " ")
    awbs = AWB_RE.findall(s)
    mawb = re.search(r"MAWB ([A-Z]{2}\d{8})", line)
    s = AWB_RE.sub(" ", s)
    lane = (LANE_RE.search(s) or [None])[0]
    s = LANE_RE.sub(" ", s)
    dates = DATE_RE.findall(s)
    s = DATE_RE.sub(" ", s)
    s = re.sub(r"(?<!\S)-(?!\S)", " ", s)                       # standalone dashes (empty columns)
    if _LATE.search(s):
        m = _LATE_DET.search(line)
        if not m:
            return None
        # the fee is the money figure equal to base x rate (its column moves between layouts)
        want = cents(dec(m[2]) * dec(m[4]) / 100)
        figs = [clean_num(x) for x in re.findall(r"(?<![\d.])\d[\d,]*\.\d{2}(?![\d%])", line[m.end():])]
        amount = next((f for f in figs if abs(dec(f) - want) <= Decimal("0.01")), None)
        if amount is None:
            return None
        return {"ref": f"p{page}.{li}", "description": "Late fee", "awb": None, "master_awb": None, "date": None,
                "lane": None, "qty": "1", "unit": "month", "rate": m[4] + "%",
                "amount": amount, "reference": None,
                "late_fee": {"on_invoice": m[1] if m else None, "month": int(m[3]) if m else None,
                            "base": clean_num(m[2]) if m else None, "rate_pct": m[4] if m else None}}
    first = re.search(r"(?<![\w.])-?\d", s)
    desc = s[:first.start()].strip() if first else s.strip()
    rest = s[first.start():] if first else ""
    nums = _tokens_numbers(rest)
    if has_sac:
        for i, (v, pct, u) in enumerate(nums):
            if re.fullmatch(r"\d{6}", v) and not pct:
                del nums[i]
                break
    if len(nums) != len(order) or not desc:
        return None
    got = dict(zip(order, nums))
    q, r, a = got["qty"], got["rate"], got["amount"]
    return {"ref": f"p{page}.{li}", "description": desc, "awb": awbs[0] if awbs else None,
            "master_awb": mawb[1] if mawb else None, "date": to_iso(dates[0]) if dates else None, "lane": lane,
            "qty": q[0], "unit": q[2], "rate": r[0] + ("%" if r[1] else ""), "amount": a[0], "reference": reference,
            "late_fee": None}


def _arith_ok(ln: dict) -> bool | None:
    q, r, a = dec(ln["qty"]), dec(ln["rate"]), dec(ln["amount"])
    if q is None or r is None or a is None:
        return None
    exp = cents(q * r / 100) if str(ln["rate"]).endswith("%") else cents(q * r)
    return abs(exp - a) <= Decimal("0.01")


def parse_billing(doc: dict) -> dict | None:
    pages = doc["pages"]
    text = "\n".join(pages)
    head = "\n".join(l for _, _, l in list(lines_of(doc))[:14])
    t = _TITLE.search(head)
    if not t:
        return None
    kind = t[1].lower()
    doc_type = ("credit_note" if "credit" in kind else "debit_note" if "debit" in kind else
                "statement" if "statement" in kind else "invoice")
    d = {"doc_type": doc_type, "number": t[2], "issue_date": None, "due_date": None, "currency": None,
         "contract_ref": None, "issuer": (pages[0].split("\n")[1] if doc["kind"] == "xlsx" else pages[0].split("\n")[0]),
         "bill_to": None, "status_marker": None, "credit_type": None, "credit_period": None,
         "applies_to_invoice": None, "lines": [], "subtotal": None, "tax_total": None, "total": None}
    flat = text.replace(" | ", " ")
    if m := _ISSUE.search(flat): d["issue_date"] = to_iso(m[1])
    if m := _DUE.search(flat): d["due_date"] = to_iso(m[1])
    if m := _CREF.search(flat): d["contract_ref"] = m[1]
    if m := re.search(r"(?i)\bbill to\b\s*(.+)|\bcustomer\b\s+(.+)|\baccount\b\s+(.+)", flat): d["bill_to"] = (m[1] or m[2] or m[3]).strip()
    if m := re.search(r"\b(COPY|DUPLICATE|REMINDER|FOR YOUR RECORDS|PAID)\b", text): d["status_marker"] = m[1]
    if m := re.search(r"\((USD|INR|EUR|GBP)\)|(?i:currency)\s+([A-Z]{3})\b|(?i:total[a-z ]*)\s+(USD|INR|EUR|GBP)\b", flat):
        d["currency"] = next(g for g in m.groups() if g)
    if not (d["number"] and d["issue_date"] and d["currency"]):
        return None
    # totals
    for _, _, ln in lines_of(doc):
        s = ln.replace(" | ", " ").strip()
        m = MONEY_END.search(s) or re.search(r"([\d,]+\.\d{2})\s*$", s)
        if not m:
            continue
        amt = clean_num(m[1])
        if re.match(r"(?i)(sub-?total|net charges|charges\b)", s) and not d["subtotal"]:
            d["subtotal"] = amt
        elif re.match(r"(?i)(tax|gst|vat)\b", s) and not d["tax_total"]:
            d["tax_total"] = amt
        elif re.match(r"(?i)(total|balance\s*due|amount\s*due)", s):
            d["total"] = amt
    if doc_type == "credit_note":
        m = re.search(r"(?i)credit type\s+(\w+)\s*\|?\s*period\s+(\S+)", flat)
        if not m or not d["total"]:
            return None
        d["credit_type"], d["credit_period"] = m[1].lower(), m[2]
        if m := re.search(r"(?i)(?:against|applies to|relates to|for) invoice\s+(\S+)", flat):
            d["applies_to_invoice"] = m[1]
        return d
    # lines
    if doc["kind"] == "xlsx":
        d["lines"] = _xlsx_lines(doc)
    else:
        d["lines"] = _pdf_lines(doc)
    if d["lines"] is None or not d["lines"]:
        return None
    for ln in d["lines"]:
        ln["flags"] = [] if _arith_ok(ln) is not False else ["arith"]
    total_lines = sum(dec(l["amount"]) for l in d["lines"])
    expect = dec(d["subtotal"]) if d["subtotal"] else (dec(d["total"]) - (dec(d["tax_total"]) or 0) if d["total"] else None)
    if expect is None or abs(total_lines - expect) > Decimal("0.01"):
        return None                                  # lines don't add up to the document's own subtotal: don't trust
    return d


def _pdf_lines(doc: dict):
    out, order, hdr = [], None, ""
    for pi, li, ln in lines_of(doc):
        if order is None:
            if _is_header(ln):
                order, hdr = _col_order(ln), ln.lower()
            continue
        if re.match(r"(?i)^(sub-?total|net\s*charges|charges\s*[A-Z]{3}|total|tax|gst|vat|balance\s*due|amount\s*due)", ln.strip()):
            break                                                     # the summary rows: the table is over
        if len(_tokens_numbers(ln)) < 2 or re.match(r"(?i)^(page\s*\d+\s*of\s*\d+|payment\s*terms|remit|terms|settlement|bank)", ln.strip()) \
                or re.search(r"(?i)\(continued\)", ln):
            continue                                                  # page furniture and payment instructions
        r = _parse_pdf_line(pi, li, ln, order, bool(re.match(r"(?i)^no\.?\b", hdr)), "sac" in hdr)
        if r is None:
            return None
        out.append(r)
    return out if order else None


_XROLE = [("description", r"charge|description"), ("reference", r"reference|period"), ("qty", r"quantity|qty|basis|units"),
          ("rate", r"rate|price"), ("amount", r"amount|net")]


def _xlsx_lines(doc: dict):
    out, cols = [], None
    for pi, li, ln in lines_of(doc):
        cells = [c.strip() for c in ln.split(" | ")]
        if cols is None:
            roles = []
            for c in cells:
                roles.append(next((r for r, p in _XROLE if re.search(p, c.lower())), None))
            if {"description", "qty", "rate", "amount"} <= set(roles):
                cols = roles
            continue
        if len(cells) != len(cols) or re.match(r"(?i)^total", cells[0]):
            if re.match(r"(?i)^total", cells[0]):
                break
            continue
        r = dict(zip(cols, cells))
        qm = re.match(r"(-?[\d,]+\.?\d*)\s*([a-z]+)?", r["qty"].lower())
        if not qm or dec(r["rate"]) is None or dec(r["amount"]) is None:
            return None
        out.append({"ref": f"p{pi}.{li}", "description": r["description"], "awb": None, "master_awb": None, "date": None,
                    "lane": None, "qty": clean_num(qm[1]), "unit": qm[2], "rate": clean_num(r["rate"]),
                    "amount": clean_num(r["amount"]), "reference": r.get("reference"), "late_fee": None})
    return out if cols else None


# ======================================================================================================== contracts ======
def _clauses(doc: dict):
    """[(ref, text)] one per numbered clause, wrapped lines joined; page furniture dropped."""
    out, cur = [], None
    for pi, li, ln in lines_of(doc):
        s = ln.strip()
        if s.startswith("Confidential |") or s.startswith("Initials:"):
            continue
        if re.match(r"^\d+(\.\d+)*\s+[A-Z]", s):
            cur = [f"p{pi}.{li}", s]
            out.append(cur)
        elif cur is not None:
            cur[1] += " " + s
    return [(r, t) for r, t in out]


def _first(pattern: str, text: str, flags=0):
    m = re.search(pattern, text, flags)
    return m


def parse_contract(doc: dict, role: str) -> dict | None:
    """MSA, amendment or annex revision. Returns None when a clause family cannot be read."""
    text = "\n".join(doc["pages"])
    flat = re.sub(r"\s+", " ", text)
    d = {"role": {"MSA": "msa", "amendment": "amendment", "rate annex": "annex_revision"}[role], "contract_id": None,
         "amendment_ref": None, "amendment_no": None, "annex_name": None, "dated": None, "effective_from": None,
         "effective_to": None, "unsigned_marker": None, "shipper": None, "carrier": None, "signatories": [], "terms": {},
         "cited_documents": [], "other_changes": [], "periods": [], "rate_columns": [], "rate_rows": [], "fuel_rows": [],
         "accessorials": [], "code_map": [], "fx_rows": [], "people": []}
    if m := re.search(r"(?i)\b(DRAFT|UNSIGNED|PROPOSAL|FOR DISCUSSION|NOT FOR SIGNATURE)\b", text):
        d["unsigned_marker"] = m[1].upper()
    for pi, li, ln in lines_of(doc):
        if m := re.match(r"Name: (.+)", ln.strip()):
            sig = {"name": m[1], "title": None, "party": None, "date": None}
            d["signatories"].append(sig)
        elif d["signatories"] and (m := re.match(r"Title: (.+)", ln.strip())):
            d["signatories"][-1]["title"] = m[1]
        elif d["signatories"] and (m := re.match(r"Date: (\d{4}-\d{2}-\d{2})", ln.strip())):
            d["signatories"][-1]["date"] = m[1]
    sig_parties = re.findall(r"Signed for and on behalf of (.+?) \(the (Shipper|Carrier)\)", flat)
    for sig, (_, party) in zip(d["signatories"], sig_parties):
        sig["party"] = party
    if role == "amendment":
        return _parse_amendment(doc, d, flat)
    m = re.search(r"Agreement No\.\s*(\S+)", text)
    d["contract_id"] = m[1] if m else None
    if m := re.search(r"([^\n(]+?),? whose principal office[^;]*?\(the Shipper\)", flat): d["shipper"] = m[1].strip(" ()0123456789")
    if m := re.search(r"([^\n(]+?),? whose principal office[^;]*?\(the Carrier\)", flat): d["carrier"] = m[1].strip(" ()0123456789")
    if m := re.search(r"(?:Dated|THIS AGREEMENT is dated) (\d{4}-\d{2}-\d{2})", flat): d["dated"] = m[1]
    t = d["terms"]
    cl = _clauses(doc)
    ctext = {r: x for r, x in cl}
    allc = " ".join(x for _, x in cl)
    ed = re.search(r"Effective [Dd]ate\.\s*(.*?)(?= \d+(?:\.\d+)* [A-Z]|$)", allc)      # 'takes effect on D (the Effective Date)'
    if ed and (m := re.search(r"\d{4}-\d{2}-\d{2}", ed[1])): t["effective_date"] = m[0]
    elif m := re.search(r"Effective Date[^.]*?(\d{4}-\d{2}-\d{2})", allc): t["effective_date"] = m[1]
    d["effective_from"] = t.get("effective_date")
    if m := re.search(r"(?:ending on|expires on|until|ends on) (\d{4}-\d{2}-\d{2})", allc): t["term_end"] = m[1]
    if m := re.search(r"(?:initial term (?:is|runs for) )(\d+) months", allc): t["term_months"] = int(m[1])
    auto = re.search(r"(?i)does not (?:renew|continue)", allc)
    t["auto_renews"] = False if auto else (True if re.search(r"(?i)renews? automatically|automatically renew", allc) else None)
    if t["auto_renews"] is None or t.get("term_end") is None:
        return None
    if t["auto_renews"]:
        if m := re.search(r"(\d+) days'? (?:prior |written )?notice", allc): t["notice_days"] = int(m[1])
        else: return None
    if m := re.search(r"(?:due|payable) (\d+) days (?:after|from)", allc): t["payment_days"] = int(m[1])
    if m := re.search(r"(?:more than|for more than|beyond) (\d+) days (?:after|past) the due date.*?late fee of ([\d.]+)% per month", allc):
        t["late_grace_days"], t["late_fee_pct_per_month"] = int(m[1]), m[2]
    if re.search(r"principal plus unpaid", allc): t["late_compounding"] = True
    elif re.search(r"(?i)(simple|principal only|does not compound|no compounding)", allc): t["late_compounding"] = False
    if m := re.search(r"- (\d+) free days", allc): t["storage_free_days"] = int(m[1])
    tiers = re.findall(r"QS >= USD ([\d,]+\.\d{2}): ([\d.]+)%", allc)
    t["rebate_tiers"] = [{"qs_min": clean_num(a), "pct": b} for a, b in tiers]
    if m := re.search(r"(?:within|no later than) (\d+) days after (?:the end of each )?[Qq]uarter", allc):
        t["rebate_due_days_after_quarter_end"] = int(m[1])
    if m := re.search(r"Qualifying Spend means freight charges \(([^)]*)\)", allc):
        t["qs_includes"] = [x.strip() for x in re.split(r",| and ", m[1]) if x.strip()]
    if m := re.search(r"It excludes (.+?)\. ", allc + " "):
        t["qs_excludes"] = [x.strip() for x in re.split(r",(?![^(]*\))| and ", m[1]) if x.strip()]
    mg = re.search(r"Handling margin\.\s*(.*?)(?= \d+\.\d+ |$)", allc)
    t["margin_applicable"] = False if (mg and re.search(r"(?i)not applicable", mg[1])) else None
    if t["margin_applicable"] is None and mg:
        return None                                           # a real margin clause: wording unknown, let the model read it
    # clause families the engine relies on: read them by meaning, and decline when one is present but unreadable
    ac = re.search(r"Annual improvement credit\.\s*(.*?)(?= \d+(?:\.\d+)* [A-Z]|$)", allc)
    if ac and not re.search(r"(?i)not applicable", ac[1]):
        pm = re.search(r"([\d.]+)%", ac[1])
        if not pm:
            return None
        t["annual_credit_pct"], t["annual_credit_basis"] = pm[1], re.sub(r"^.*?%\s*(?:of\s+)?", "", ac[1]).strip(" .")
        if m2 := re.search(r"(\d+) days after", ac[1]):
            t["annual_credit_due_days"] = int(m2[1])
    if re.search(r"Volume rebate\.", allc) and not t["rebate_tiers"] and not re.search(r"Volume rebate\.\s*Not applicable", allc):
        return None
    if m := re.search(r"Precedence\.\s*([^.]*(?:>[^.]*)+)\.", allc):
        t["precedence"] = [x.strip() for x in m[1].split(">")]
    t["correspondence_ranked"] = False if re.search(r"does not rank correspondence", allc) else None
    if m := re.search(r"within (\d+) days of the invoice date", allc): t["dispute_window_days"] = int(m[1])
    if m := re.search(r"applied to the (base freight)", allc): t["fuel_applies_to"] = m[1]
    if not all(t.get(k) is not None for k in ("payment_days", "late_fee_pct_per_month", "late_grace_days")):
        return None
    _contract_tables(doc, d)
    if not (d["rate_rows"] and d["fuel_rows"] and d["people"] and d["code_map"] and d["accessorials"]):
        return None
    # every priced number must really be in its line: a table read wrongly is worse than a model call
    return d


_MONTHS_LONG = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december"]


def _contract_tables(doc: dict, d: dict) -> None:
    prev = None
    in_fx = False
    for pi, li, ln in lines_of(doc):
        s = ln.strip()
        ref = f"p{pi}.{li}"
        if m := re.match(r"Period (\w+): pickups (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", s):
            d["periods"].append({"name": m[1], "from": m[2], "to": m[3]})
        elif re.match(r"Lane\s+Weight band\s+", s):
            d["rate_columns"] = re.findall(r"(\w+) rate", s) or re.findall(r"\b(Rate)\b", s)
        elif m := re.match(r"^(\S+-\S+) (minimum charge|(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?) kg|(\d+(?:\.\d+)?)\+ kg) ((?:[\d.]+ ?)+)(USD|INR)?$", s):
            vals = m[6].split()
            row = {"ref": ref, "lane": m[1], "kind": "minimum_charge" if m[2] == "minimum charge" else "rate",
                   "band_min": m[3] or m[5], "band_max": m[4], "currency": m[7] or "USD",
                   "v1": vals[0] if vals else None, "v2": vals[1] if len(vals) > 1 else None,
                   "v3": vals[2] if len(vals) > 2 else None, "v4": vals[3] if len(vals) > 3 else None}
            d["rate_rows"].append(row)
        elif m := re.match(r"^(\d{4}-\d{2}) ([\d.]+)%$", s):
            d["fuel_rows"].append({"ref": ref, "month": m[1], "pct": m[2] + "%"})
        elif re.match(r"^Annex F", s):
            in_fx = True
        elif re.match(r"^Schedule 1", s):
            in_fx = False
        elif in_fx and (m := re.match(r"^(\d{4}-\d{2}) ([\d.]+)$", s)):
            d["fx_rows"].append({"ref": ref, "month": m[1], "inr_per_usd": m[2]})
        elif m := re.match(r"^Code (\w+): (.+?); also billed as: (.+)$", s):
            d["code_map"].append({"ref": ref, "code": m[1], "label": m[2], "aliases": [a.strip() for a in m[3].split(",")]})
        elif m := re.match(r"^([A-Z][A-Z0-9]+) (.+?) ([\d,]+\.\d{2}) (USD|INR) (per .+|for .+)$", s):
            prev = {"ref": ref, "code": m[1], "label": m[2], "fee": clean_num(m[3]), "currency": m[4], "basis": m[5],
                    "condition": None}
            d["accessorials"].append(prev)
        elif m := re.match(r"^(.+?), (.+?), ([A-Z0-9-]+): ([a-z_, ]+) <(\S+@\S+)>$", s):
            d["people"].append({"ref": ref, "name": m[1], "title": m[2], "party": m[3],
                                "categories": [c.strip() for c in m[4].split(",")], "email": m[5].lower()})
        elif prev and re.match(r"^[A-Za-z ]+ only$|^[A-Z][a-z]+ to [A-Z][a-z]+", s) and prev["ref"].split(".")[0] == ref.split(".")[0]:
            prev["condition"] = s                         # 'September to November only': the basis wraps onto this line


def _parse_amendment(doc: dict, d: dict, flat: str) -> dict | None:
    if m := re.search(r"Amendment reference (\S+)", flat):
        d["amendment_ref"] = m[1]
        d["contract_id"] = m[1].split("/")[0]
    if m := re.search(r"Amendment No\.? (\d+)", flat): d["amendment_no"] = m[1]
    if m := re.search(r"Effective from (\d{4}-\d{2}-\d{2})", flat): d["effective_from"] = m[1]
    if not (d["contract_id"] and d["effective_from"]):
        return None
    d["rate_columns"] = ["Rate"]
    for pi, li, ln in lines_of(doc):
        s = ln.strip()
        if m := re.match(r"^(\S+-\S+) (minimum charge|(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?) kg|(\d+(?:\.\d+)?)\+ kg) ([\d.]+) (USD|INR)$", s):
            d["rate_rows"].append({"ref": f"p{pi}.{li}", "lane": m[1],
                                   "kind": "minimum_charge" if m[2] == "minimum charge" else "rate",
                                   "band_min": m[3] or m[5], "band_max": m[4], "currency": m[7], "v1": m[6], "v2": None,
                                   "v3": None, "v4": None})
    for m in re.finditer(r"(Annex [A-F] rev \d+)(?: \(attached\))? applies from (\d{4}-\d{2}-\d{2}) to lane\(s\) ([A-Z0-9, -]+?)(?=\.|$)", flat):
        d["cited_documents"].append({"ref": None, "name": m[1], "attached": "(attached)" in m[0],
                                     "applies_from": m[2], "lanes": [x.strip() for x in m[3].split(",")]})
    if m := re.search(r"(?i)\bextend\w*[^.]*?(?:ends? on|until|expires? on)\s+(\d{4}-\d{2}-\d{2})", flat):
        d["terms"]["new_term_end"] = m[1]
        d["other_changes"].append({"ref": None, "text": m[0]})
    known = len(d["rate_rows"]) + len(d["cited_documents"]) + len(d["other_changes"])
    if re.search(r"(?i)\brenew|\bterminat", flat) or known == 0:
        return None                                  # an amendment that changes something this parser doesn't know
    return d


# ======================================================================================================= records =========
_AP = [("paid_date", r"paid|settled"), ("due_date", r"\bdue"), ("invoice_date", r"invoice date|issued|dated|date"),
       ("status", r"status|state"), ("amount", r"amount|total|value|balance"),
       ("invoice_no", r"invoice no|invoice number|invoice|document|reference|ref")]
_TR = [("reason_code", r"reason"), ("station", r"station|location|hub|port"), ("date", r"date|time"),
       ("event", r"event|milestone|status"), ("awb", r"awb|waybill|shipment")]


def _map_header(cells: list[str], table) -> list[str]:
    return [next((c for c, p in table if re.search(p, cell.lower())), "skip") for cell in cells]


def parse_table(doc: dict) -> tuple[str, dict] | None:
    """tracking log or AP register by header cell names. None when the header isn't recognised."""
    from extraction import parse_table as rows_of          # the shared row parser (positional, normalising)
    first = [l for l in doc["pages"][0].split("\n")]
    for li, ln in enumerate(first[:8], 1):
        cells = [c.strip() for c in ln.split(" | ")]
        if len(cells) < 4:
            continue
        tr, ap = _map_header(cells, _TR), _map_header(cells, _AP)
        if {"awb", "event", "date"} <= set(tr) and "invoice_no" not in ap[:1]:
            return "tracking", {"rows": rows_of(doc["pages"], "tracking", li, tr), "as_of": None}
        if {"invoice_no", "amount", "status"} <= set(ap) and "due_date" in ap:
            return "ap_register", {"rows": rows_of(doc["pages"], "ap_register", li, ap), "as_of": None}
    return None


def parse_awb(doc: dict) -> dict | None:
    text = "\n".join(doc["pages"])
    m = re.search(r"(?i)(?:master )?air waybill ([A-Z]{2}\d{8})", text)
    if not m:
        return None
    out = {"awb": m[1], "master_awb": None, "executed_date": None, "origin": None, "destination": None, "pieces": None,
           "actual_weight_kg": None, "chargeable_weight_kg": None, "dims": [], "goods": None, "houses": [], "refs": []}
    if re.search(r"(?i)master air waybill", text):
        out["master_awb"] = m[1]
    if x := re.search(r"Executed on (\d{4}-\d{2}-\d{2})", text): out["executed_date"] = x[1]
    if x := re.search(r"Origin (\S+) \| Destination (\S+)", text): out["origin"], out["destination"] = x[1], x[2]
    if x := re.search(r"(?:Total pieces|Pieces) (\d+)", text): out["pieces"] = x[1]
    if x := re.search(r"Actual weight ([\d.]+) kg", text): out["actual_weight_kg"] = x[1]
    if x := re.search(r"(?:Total chargeable weight|Chargeable weight) ([\d.]+) kg", text): out["chargeable_weight_kg"] = x[1]
    if x := re.search(r"Dimensions (\d+)x(\d+)x(\d+) cm x (\d+)", text):
        out["dims"] = [{"l": x[1], "w": x[2], "h": x[3], "count": x[4]}]
    if x := re.search(r"Nature of goods: (.+)", text): out["goods"] = x[1].strip()
    if x := re.search(r"Handling information: (.+)", text): out["handling"] = x[1].strip()
    for x in re.finditer(r"House (\S+): (\d+) pieces?, ([\d.]+) kg", text):
        out["houses"].append({"awb": x[1], "pieces": x[2], "chargeable_weight_kg": x[3]})
    if not (out["chargeable_weight_kg"] or out["houses"]):
        return None
    return out


def parse_dgd(doc: dict) -> dict | None:
    text = "\n".join(doc["pages"])
    m = re.search(r"(?i)declaration for AWB ([A-Z]{2}\d{8})", text)
    if not m:
        return None
    out = {"awb": m[1], "goods": None, "items": [], "refs": []}
    if x := re.search(r"Goods: (.+)", text): out["goods"] = x[1].strip()
    for pi, li, ln in lines_of(doc):
        if x := re.match(r"UN(\d{4}) (.+?)(?: - (.+))?$", ln.strip()):
            purpose = x[3]
            q = re.search(r"\s([\d.]+ ?kg)$", x[2])
            out["items"].append({"un_number": x[1], "description": x[2][:q.start()] if q else x[2],
                                 "quantity": q[1] if q else None, "purpose": purpose,
                                 "is_dangerous_goods": not (purpose and re.search(r"(?i)refrigerant", purpose)),
                                 "ref": f"p{pi}.{li}"})
    return out if out["items"] else None


# ======================================================================================================== emails =========
_COND = re.compile(r"(?i)\b(if|provided|subject to|conditional|unless|only|once|this time|one[- ]off|future|going forward|"
                   r"each|every|all|until|when)\b")
_ROW = re.compile(r"(?P<lane>\b[A-Z]{2,4}\d-[A-Z]{2,4}\d)\s+(?P<band>minimum charge|\d+(?:\.\d+)?-\d+(?:\.\d+)?\s*kg|"
                  r"\d+(?:\.\d+)?\+\s*kg)\s*[:=]\s*(?:(?P<c1>USD|INR)\s*)?(?P<v>\d+(?:\.\d+)?)(?:\s*(?P<c2>USD|INR))?")
_EVENTS = [("PICKED_UP", r"picked up"), ("DELIVERED", r"delivered"), ("ARRIVED_FACILITY", r"arrived"),
           ("OUT_FOR_DELIVERY", r"out for delivery"), ("COLLECTED_BY_CONSIGNEE", r"collected by (?:the )?consignee"),
           ("DEPARTED", r"departed")]


def _blank(**kw):
    a = {"type": None, "refs": [], "summary": None, "awbs": [], "invoice_ids": [], "charge": None, "months": [],
         "lanes": [], "scope_words": None, "condition": None, "effective_from": None, "effective_to": None,
         "new_due_date": None, "event": None, "event_date": None, "station": None, "reason": None, "rates": [],
         "reference": None, "contract_id": None, "in_quoted_text": False, "original_author": None,
         "addressed_to_automation": None}
    a.update(kw)
    return a


_SKIP = re.compile(r"(?i)^(hi|hello|dear|good (morning|afternoon|evening)|to whom|thanks|thank you|many thanks|with thanks|"
                   r"regards|best|kind|warm|sincerely|yours|please find attached|attached|reference:|notice ref)")
_NOISE = re.compile(r"(?i)\b(waiv|extend|extension|rate|notice|picked up|deliver|arriv|hold|releas|credit|refund|cancel|"
                    r"disregard|short-?pay|approv|ignor|instruct|automat|assistant|\bAI\b|auditor|\bpay|paid|dispute|"
                    r"outstanding|falls due|late fee|pursue)")


def parse_email(doc: dict) -> dict | None:
    page = doc["pages"][0]
    lines = page.split("\n")
    q = quoted_lines(page)
    body = [(i, l.strip()) for i, l in enumerate(lines, 1) if i > 5 and i not in q and l.strip()]
    out, used = [], set()
    # rate notice: rows lane + band [:=] value
    rows = [(i, m) for i, l in body for m in _ROW.finditer(l)]
    if rows:
        first = rows[0][0]
        ctx = " ".join(l for i, l in body if i <= first)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", ID_RE.sub(" ", ctx))          # a reference id can contain a date
        ref = re.search(r"(?i)(?:reference|notice ref\.?):?\s+(\S+)", ctx)
        a = _blank(type="rate_notice", refs=[f"p1.{i}" for i, _ in rows], lanes=sorted({m["lane"] for _, m in rows}),
                   effective_from=dates[0] if dates else None, effective_to=dates[1] if len(dates) > 1 else None,
                   reference=ref[1] if ref else None, summary=" ".join(l for i, l in body if i <= first)[:200])
        for _, m in rows:
            band = m["band"].lower()
            bm = re.match(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", band)
            pm = re.match(r"(\d+(?:\.\d+)?)\+", band)
            a["rates"].append({"lane": m["lane"], "kind": "minimum_charge" if band == "minimum charge" else "rate",
                               "band_min": bm[1] if bm else (pm[1] if pm else None), "band_max": bm[2] if bm else None,
                               "value": m["v"], "currency": m["c1"] or m["c2"] or "USD"})
        if len(dates) < 1:
            return None
        out.append(a)
        used |= {(i, x.strip()) for i, l in body if i <= rows[-1][0] for x in re.split(r"(?<=[.!?])\s+", l) if x.strip()}
    sents = [(i, x.strip()) for i, l in body for x in re.split(r"(?<=[.!?])\s+", l) if x.strip()]
    for i, l in sents:
        pay = re.search(r"(?i)\b(?:process|make|arrange) (?:the |your )?payment|(?:kindly|please) pay\b|pay (?:it |this )?by\b|"
                        r"payment (?:is )?(?:due|requested)|our records show invoice .* as outstanding|reminder", l)
        if (i, l) in used or (_SKIP.match(l) and not pay):
            continue
        awbs = AWB_RE.findall(l)
        ids = [x for x in ID_RE.findall(l) if not AWB_RE.fullmatch(x)]
        dts = re.findall(r"\d{4}-\d{2}-\d{2}", l)
        ref = f"p1.{i}"
        if re.search(r"(?i)\bwaiv", l):
            if _COND.search(re.sub(r"(?i)\bif it has already been invoiced\b", "", l)):
                return None                                # conditional or scoped: a model should read it
            ctxawb = awbs or AWB_RE.findall(" ".join(t for _, t in body))
            ch = re.search(r"(?i)\b((?:[A-Z][a-z]+ )?(?:[A-Za-z]+ )?(?:fee|surcharge|charge))\b", l)
            months = re.findall(r"\b20\d{2}-\d{2}\b", l)
            if not (ctxawb or months):
                return None                                    # names neither a shipment nor a month: a model should read it
            out.append(_blank(type="waiver", refs=[ref], summary=l, awbs=sorted(set(ctxawb)), charge=ch[1] if ch else None,
                              months=months))
        elif re.search(r"(?i)\bextend|falls due|now due", l) and ids and dts:
            out.append(_blank(type="due_date_extension", refs=[ref], summary=l, invoice_ids=ids, new_due_date=dts[-1]))
        elif awbs and re.search(r"(?i)customs hold", l) and dts:
            out.append(_blank(type="customs_hold", refs=[ref], summary=l, awbs=awbs, event_date=dts[0],
                              station=(re.search(r"\bat ([A-Z]{3}\d)\b", l) or [None, None])[1]))
        elif awbs and any(re.search(p, l, re.I) for _, p in _EVENTS) and dts:
            ev = next(e for e, p in _EVENTS if re.search(p, l, re.I))
            out.append(_blank(type="milestone", refs=[ref], summary=l, awbs=awbs, event=ev, event_date=dts[0],
                              station=(re.search(r"\bat ([A-Z]{3}\d)\b", l) or [None, None])[1]))
        elif m := re.match(r"(?i)reason code (\w+):? ?(.*)", l):
            for a in out:
                if a["type"] == "customs_hold":
                    a["reason"] = f"{m[1]}: {m[2]}".strip(": ")
                    a["refs"].append(ref)
        elif m := re.match(r"(?i)released on (\d{4}-\d{2}-\d{2})", l):
            for a in out:
                if a["type"] == "customs_hold":
                    a["effective_to"] = m[1]
                    a["refs"].append(ref)
        elif pay:
            out.append(_blank(type="payment_request", refs=[ref], summary=l,
                              invoice_ids=[x for x in ID_RE.findall(l) if not AWB_RE.fullmatch(x)]))
        elif m := re.search(r"(?i)could you check the (.+?) on AWB ([A-Z]{2}\d{8})", l):
            out.append(_blank(type="question", refs=[ref], summary=l, awbs=[m[2]], charge=m[1]))
        elif _NOISE.search(l):                             # meaningful and not understood: a model should read it
            if out and re.match(r"(?i)^(no late fee|payment by that date|no credit note|simply leave|please disregard|"
                                r"if it has already been invoiced|we will not pursue|thanks for flagging)", l):
                continue
            return None
    out += _quoted_waivers(lines, q)
    return {"assertions": out}


def _quoted_waivers(lines: list[str], q: set[int]) -> list[dict]:
    """A waiver that appears only inside quoted or forwarded text: kept as a quoted assertion with who wrote the original."""
    out, author = [], None
    for i, raw in enumerate(lines, 1):
        s = re.sub(r"^[>\s]+", "", raw).strip()
        if m := re.match(r"(?i)on .{3,60}?,? (.+?) wrote:", s):
            author = m[1].strip()
        elif m := re.match(r"(?i)from:\s*(.+?)\s*<", s):
            author = m[1].strip()
        if i in q and i > 5 and re.search(r"(?i)\bwaiv", s):
            awbs = AWB_RE.findall(s)
            months = re.findall(r"\b20\d{2}-\d{2}\b", s)
            ch = re.search(r"(?i)\b((?:[A-Z][a-z]+ )?(?:[A-Za-z]+ )?(?:fee|surcharge|charge))\b", s)
            if awbs or months:
                out.append(_blank(type="waiver", refs=[f"p1.{i}"], summary=s, awbs=sorted(set(awbs)), months=months,
                                  charge=ch[1] if ch else None, in_quoted_text=True, original_author=author))
    return out


# ===================================================================================================== entry points ======
def parse(doc: dict, kind: str) -> dict | None:
    try:
        if kind == "billing": return parse_billing(doc)
        if kind == "contract": return parse_contract(doc, "MSA")
        if kind == "amendment": return parse_contract(doc, "amendment")
        if kind == "awb": return parse_awb(doc)
        if kind == "dgd": return parse_dgd(doc)
        if kind == "email": return parse_email(doc)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None                                   # a parser that trips is a parser that doesn't know: ask the model
    return None
