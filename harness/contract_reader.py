"""Read explicit contract structure without interpreting precedence or conditions.

Numbered clauses remain verbatim. Literal numeric/date operands are available to
the existing source-backed audit evaluator; the reader makes no legal decision.
Unrecognized annex layouts decline the whole contract, never just its hard rows.
"""
from __future__ import annotations

import re

from .claude_extractor import ExtractionInput
from .extraction_values import ISO_DATE, NUMBER, number, observed


def literals(text: str) -> list[dict]:
    values = []
    for match in re.finditer(ISO_DATE, text):
        values.append({"name": f"date_{len(values) + 1}", "raw": match[0], "date_value": observed(match[0])})
    numeric_text = re.sub(ISO_DATE, " ", text)
    numeric_text = re.sub(r"\bclause\s+\d+(?:\.\d+)?", " ", numeric_text, flags=re.I)
    token = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    for match in re.finditer(rf"(?<![\w.])({token})(%?)(?![\w.])", numeric_text):
        raw, percent = match.groups()
        values.append({"name": f"{'percent' if percent else 'number'}_{len(values) + 1}",
                       "raw": raw + percent, "number": str(number(raw)),
                       "unit": "percent" if percent else None})
    return values


def read_contract(item: ExtractionInput) -> list[dict] | None:
    doc = item.document
    if doc.source.format != "pdf" or not doc.extraction.pages:
        return None
    # Keep multi-page OCR contracts on the model path: table delimiters and
    # signature blocks have not been proven reliable after OCR normalization.
    if any(page.method != "grader" for page in doc.extraction.pages):
        return None
    text = "\n".join(p.native_text for p in doc.extraction.pages)
    identities = re.findall(r"^Agreement No\.\s+(\S+)\s*$", text, re.M)
    if len(identities) != 1 or not re.search(r"^Master Services Agreement[^\n]*$", text, re.M):
        return None
    cid = identities[0]

    def common(page):
        return {"confidence": 0.97, "evidence": [{"doc": doc.source.doc_ref, "page": page.number,
                                                  "quote": page.native_text, "layer": "native"}]}

    def term(page, category, raw, *, values=None, scope=None, clause=None, conditions=None):
        provenance = common(page)
        if (len("".join(raw.split())) >= 12 and
                " ".join(raw.split()) in " ".join(page.native_text.split())):
            quotes = [raw]
            if scope and scope.get("period") in periods:
                quotes.extend(line for line in page.native_text.splitlines()
                              if line.startswith("Period " + scope["period"] + ":"))
            if category in ("rate", "fuel", "accessorial", "charge_alias"):
                quotes.extend(line for line in page.native_text.splitlines()
                              if line.startswith(("Annex ", "Lane Weight band", "Month Fuel", "Code Accessorial")))
            provenance["evidence"] = [{"doc": doc.source.doc_ref, "page": page.number,
                                       "quote": quote, "layer": "native"} for quote in quotes]
        return {**provenance, "kind": "term", "contract_id": cid, "category": category,
                "clause": clause, "text": raw, "values": literals(raw) if values is None else values,
                "scope": scope or {}, "conditions": conditions or []}

    dates = re.findall(rf"(?:takes effect on|Effective Date[^\n]*? is) ({ISO_DATE})", text, re.I)
    expiry = re.findall(rf"initial term[^\n]*(?:ending on|expires on) ({ISO_DATE})", text, re.I)
    signed = re.findall(rf"Signed for and on behalf of ([^\n]+) \(the (Shipper|Carrier)\)\nSignature:[^\n]*\nName: ([^\n]+)\nTitle: [^\n]+\nDate: ({ISO_DATE})", text)
    if len(dates) != 1 or len(expiry) > 1 or len(signed) != 2 or {s[1] for s in signed} != {"Shipper", "Carrier"}:
        return None
    # A printed signature block alone is not proof of execution.
    if "the parties have executed this Agreement" not in text or re.search(r"\b(?:DRAFT|UNSIGNED|PROPOSAL)\b", text):
        return None
    evidence = [common(p)["evidence"][0] for p in doc.extraction.pages if
                "Agreement No." in p.native_text or "takes effect on" in p.native_text or
                "ending on" in p.native_text or "Signed for and on behalf of" in p.native_text]
    header = {"confidence": 0.97, "evidence": evidence, "kind": "contract", "contract_id": cid,
              "parties": [{"name": s[0], "role": s[1]} for s in signed],
              "signatories": [s[2] for s in signed], "signature_status": "signed",
              "effective_date": observed(dates[0]), "expiry_date": observed(expiry[0]) if expiry else None}
    cover_dates = re.findall(rf"^Dated ({ISO_DATE})$", text, re.M)
    if len(cover_dates) == 1:
        header["agreement_date"] = observed(cover_dates[0])
    facts = [header]
    periods = {}
    for match in re.finditer(rf"^Period (\S+): pickups ({ISO_DATE}) to ({ISO_DATE})\s*$", text, re.M):
        if match[1] in periods:
            return None
        periods[match[1]] = {"kind": "period", "period": match[1],
                              "starts_on": observed(match[2]), "ends_on": observed(match[3])}
    for page in doc.extraction.pages:
        lines = [line for line in page.native_text.splitlines() if line.strip() and
                 not line.startswith(("Confidential |", "Initials:"))]
        body = "\n".join(lines)
        if any(re.match(r"Annex [A-Z]:", line) or line.startswith("Schedule 1:") for line in lines):
            # Preserve headings and non-tabular qualifiers as source context too.
            facts.append(term(page, "annex_context", body, values=[]))
            index, section = 0, None
            while index < len(lines):
                line = lines[index]
                index += 1
                if line.startswith("Annex "):
                    section = line
                    continue
                if line.startswith("Period "):
                    continue
                if line.startswith("Lane Weight band "):
                    labels = re.findall(r"(\S+) rate", line)
                    if not labels or any(label not in periods for label in labels):
                        return None
                    while index < len(lines) and not lines[index].startswith(("Annex ", "Schedule ")):
                        row = lines[index]
                        index += 1
                        match = re.fullmatch(rf"(\S+) (minimum charge|({NUMBER})-({NUMBER}) kg|({NUMBER})\+ kg) (.+)", row)
                        if match is None:
                            return None
                        lane, band, low, high, unlimited, prices = match.groups()
                        rates = prices.split()
                        if len(rates) != len(labels):
                            return None
                        currency = re.search(r"\(([A-Z]{3}) per chargeable kg", section or "")
                        if currency is None:
                            return None
                        for label, rate in zip(labels, rates):
                            values = [{"name": "minimum_charge" if band == "minimum charge" else "rate",
                                       "raw": rate, "number": str(number(rate)), "currency": currency[1],
                                       "unit": "shipment" if band == "minimum charge" else "kg"}]
                            if band != "minimum charge":
                                values.append({"name": "minimum_weight", "raw": low or unlimited,
                                               "number": str(number(low or unlimited)), "unit": "kg"})
                                if high is not None:
                                    values.append({"name": "maximum_weight", "raw": high, "number": str(number(high)), "unit": "kg"})
                            facts.append(term(page, "rate", row, values=values,
                                              scope={**periods[label], "lanes": [lane]}))
                    continue
                if line == "Month Fuel surcharge":
                    while index < len(lines) and not lines[index].startswith(("Annex ", "Schedule ")):
                        row = lines[index]
                        index += 1
                        match = re.fullmatch(rf"(\d{{4}}-\d{{2}}) ({NUMBER})%", row)
                        if match is None:
                            return None
                        observed(match[1] + "-01")
                        facts.append(term(page, "fuel", row, scope={"kind": "period", "period": match[1]},
                                          values=[{"name": "percent", "raw": match[2] + "%",
                                                   "number": str(number(match[2])), "unit": "percent"}]))
                    continue
                if line == "Month INR per 1 USD":
                    while index < len(lines) and not lines[index].startswith(("Annex ", "Schedule ")):
                        row = lines[index]
                        match = re.fullmatch(rf"(\d{{4}}-\d{{2}}) ({NUMBER})", row)
                        if match is None:
                            break
                        index += 1
                        observed(match[1] + "-01")
                        facts.append(term(page, "fx", row, scope={"kind": "period", "period": match[1]},
                                          values=[{"name": "inr_per_usd", "raw": match[2],
                                                   "number": str(number(match[2])), "unit": "INR_per_USD"}]))
                    continue
                if line == "Code Accessorial Fee Basis":
                    while index < len(lines) and not lines[index].startswith(("Annex ", "Schedule ")):
                        row = lines[index]
                        index += 1
                        if re.match(r"^[A-Z][A-Za-z ]+:", row):
                            while (index < len(lines) and not lines[index].startswith(("Annex ", "Schedule "))
                                   and re.match(r"^[A-Z0-9_]+\s+.+?\s+" + NUMBER + r"\s+[A-Z]{3}\s+", lines[index]) is None):
                                row += "\n" + lines[index]
                                index += 1
                            facts.append(term(page, "annex_condition", row))
                            continue
                        while (index < len(lines) and not lines[index].startswith(("Annex ", "Schedule "))
                               and re.match(r"^[A-Z0-9_]+\s+.+?\s+" + NUMBER + r"\s+[A-Z]{3}\s+", lines[index]) is None
                               and re.match(r"^[A-Z][A-Za-z ]+:", lines[index]) is None):
                            row += "\n" + lines[index]
                            index += 1
                        match = re.fullmatch(rf"(\S+) (.+?) ({NUMBER}) ([A-Z]{{3}}) ([^,\n]+)(?:,\s*(.+))?", row, re.S)
                        if match is None:
                            return None
                        code, label, amount, currency, basis, condition = match.groups()
                        facts.append(term(page, "accessorial", row, scope={"charge_labels": [code, label]},
                                          values=[{"name": "fee", "raw": amount, "number": str(number(amount)),
                                                   "currency": currency, "unit": basis.strip()}],
                                          conditions=[condition.strip()] if condition else []))
                    continue
                if line.startswith("Code "):
                    match = re.fullmatch(r"Code (\S+): (.+?); also billed as: (.+)", line)
                    if match is None:
                        return None
                    facts.append(term(page, "charge_alias", line, values=[
                        {"name": "code", "raw": match[1]}, {"name": "label", "raw": match[2]},
                        {"name": "aliases", "raw": match[3]}]))
                    continue
                if line.startswith("Schedule 1:"):
                    while index < len(lines):
                        row = lines[index]
                        index += 1
                        match = re.fullmatch(r"([^,]+), ([^,]+), ([^:]+): ([^<]+) <([^<>\s]+@[^<>\s]+)>", row)
                        if match is None:
                            return None
                        name, title, party, categories, email = match.groups()
                        facts.append({**common(page), "kind": "authority", "contract_id": cid,
                                      "name": name, "title": title, "party": party, "email": email,
                                      "categories": [c.strip() for c in categories.strip().split(",")]})
                    continue
                if section and re.match(r"^[A-Z]", line) and not re.search(r"\s\|\s", line):
                    paragraph = line
                    while index < len(lines) and not lines[index].startswith(("Annex ", "Schedule ")):
                        paragraph += "\n" + lines[index]
                        index += 1
                    if not paragraph.rstrip().endswith("."):
                        return None
                    facts.append(term(page, "annex_condition", paragraph))
                    continue
                if not re.match(r"^(?:Not applicable\b|Annex [A-Z]: Not applicable\b)", line):
                    # Unknown FX tables, continuation layouts or annex qualifiers
                    # need the model; do not silently drop their operands.
                    return None
        else:
            matches = list(re.finditer(r"^(\d+\.\d+)\s+", body, re.M))
            if not matches:
                # Identity, contents and execution pages remain available verbatim.
                facts.append(term(page, "source_context", body))
                continue
            if body[:matches[0].start()].strip():
                facts.append(term(page, "source_context", body[:matches[0].start()].strip()))
            for n, match in enumerate(matches):
                end = matches[n + 1].start() if n + 1 < len(matches) else len(body)
                raw = body[match.end():end].strip()
                # Retain intervening section headings and unnumbered paragraphs
                # too. They can carry qualifications outside a numbered clause.
                facts.append(term(page, "clause", raw, clause=match[1]))
    return facts
