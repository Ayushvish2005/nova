"""Conservative, source-grounded readers; decline entire documents on ambiguity.

These readers extract printed facts only. They never select contract precedence,
authorize an actor, compute an audit finding, or treat document text as commands.
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .claude_extractor import ExtractionInput, StructuredResult, digest
from .fact_validation import validate_evidence
from .facts import FactBundle
from .extraction_values import ISO_DATE, NUMBER, heading, number, observed, slash_date

VERSION = "local-facts:1"


class LocalExtractor:
    model = "deterministic"
    cache_key = VERSION + ":" + digest(FactBundle.model_json_schema())

    def close(self) -> None:
        pass

    def try_extract(self, item: ExtractionInput) -> StructuredResult | None:
        doc = item.document
        if (doc.extraction.status != "ready" or doc.source.kind != "attachment"
                or doc.source.format not in ("pdf", "xlsx")):
            return None
        from .contract_reader import read_contract
        try:
            contract = read_contract(item)
            if contract is not None:
                bundle = FactBundle.model_validate_json(json.dumps({"facts": contract}))
                validate_evidence(bundle, doc, item.known_refs)
                return StructuredResult(bundle)
        except (ValueError, InvalidOperation):
            return None
        # Other multi-page documents require the model until we have a complete
        # reader. Never publish a successfully parsed subset of a source.
        if len(doc.extraction.pages) != 1:
            return None
        page = doc.extraction.pages[0]
        layer = page.method if page.method in ("ocr", "html") else "native"
        text = page.text if layer != "native" else page.native_text
        parse_text = self._ocr_spacing(text) if layer == "ocr" else text
        evidence = [{"doc": doc.source.doc_ref, "page": page.number,
                     "quote": text, "layer": layer}]
        common = {"evidence": evidence, "confidence": 0.9 if layer == "ocr" else 0.98}
        try:
            facts = self._table(parse_text, common) if doc.source.format == "xlsx" else self._invoice(parse_text, common)
            if facts is None:
                return None
            bundle = FactBundle.model_validate_json(json.dumps({"facts": facts}))
            validate_evidence(bundle, doc, item.known_refs)
            return StructuredResult(bundle)
        except (ValueError, InvalidOperation):
            return None

    def extract(self, item: ExtractionInput) -> StructuredResult:
        result = self.try_extract(item)
        if result is None:
            raise ValueError("local reader declined document; model fallback required")
        return result

    @staticmethod
    def _ocr_spacing(text: str) -> str:
        """Repair only token boundaries proven by the recognized table grammar."""
        text = re.sub(r"\bChargesadvice\b", "Charges advice", text, flags=re.I)
        text = re.sub(r"\bPayableby\b", "Payable by", text, flags=re.I)
        text = re.sub(r"\bTerms(?=\d)", "Terms ", text, flags=re.I)
        text = re.sub(r"(?<=\d)(kg|shpt|days|month|fee)\b", r" \1", text, flags=re.I)
        text = re.sub(r"(?<=[a-z])(fee|surcharge)\b", r" \1", text, flags=re.I)
        text = re.sub(r"\b([A-Z]{3})(?=-?\d)", r"\1 ", text)
        text = re.sub(r"\b([A-Z]{2,4})\s+(\d-[A-Z]{2,4})\s+(\d)\b", r"\1\2\3", text)
        text = re.sub(r"Charges advice\s+([A-Z]{2,4})\s+(-\d+-\d+)", r"Charges advice \1\2", text)
        return text

    def _table(self, text: str, common: dict) -> list[dict] | None:
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        headers = [heading(cell) for cell in lines[0].split("|")]
        aliases = {
            "awb": "shipment", "waybill": "shipment", "shipmentid": "shipment",
            "event": "event", "date": "date", "eventdate": "date",
            "station": "station", "reasoncode": "reason", "reason": "reason",
            "invoiceno": "invoice", "invoicenumber": "invoice", "invoice": "invoice",
            "invoicedate": "invoice_date", "duedate": "due_date", "paiddate": "paid_date",
            "amount": "amount", "status": "status", "currency": "currency",
        }
        columns = [aliases.get(h) for h in headers]
        if None in columns or len(columns) != len(set(columns)):
            return None
        tracking = {"shipment", "event", "date"} <= set(columns)
        payment = {"invoice", "status", "amount"} <= set(columns)
        if tracking == payment:
            return None
        allowed = ({"shipment", "event", "date", "station", "reason"} if tracking else
                   {"invoice", "status", "amount", "invoice_date", "due_date", "paid_date", "currency"})
        if not set(columns) <= allowed:
            return None
        facts = []
        for line in lines[1:]:
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) != len(columns):
                return None
            row = dict(zip(columns, cells))
            if tracking:
                if not row["shipment"] or not re.fullmatch(r"[A-Z][A-Z_ ]+", row["event"]):
                    return None
                facts.append({**common, "kind": "shipment", "shipment_id": row["shipment"],
                              "record_type": "tracking_event", "event": row["event"],
                              "event_date": observed(row["date"]),
                              "station": row.get("station") or None,
                              "reason": None if row.get("reason") in (None, "", "-") else row["reason"]})
            else:
                if not row["invoice"] or row["status"].lower() not in {"paid", "unpaid", "disputed", "open", "partially paid"}:
                    return None
                details = []
                if row.get("invoice_date") not in (None, "", "-"):
                    details.append({"name": "invoice_date", "raw": row["invoice_date"],
                                    "date_value": observed(row["invoice_date"])})
                facts.append({**common, "kind": "payment", "invoice_number": row["invoice"],
                              "status": row["status"], "amount": str(number(row["amount"])),
                              "currency": row.get("currency") or None, "details": details,
                              **{key: observed(row[key]) if row.get(key) not in (None, "", "-") else None
                                 for key in ("due_date", "paid_date")}})
        return facts

    def _invoice(self, text: str, common: dict) -> list[dict] | None:
        if re.search(r"^Charges advice\s+", text, re.M | re.I):
            return self._charges_advice(text, common)
        # This grammar is structural: no customer, carrier, lane, code, price,
        # invoice id or expected answer is embedded in the implementation.
        identity = re.findall(r"^INVOICE\s+([^\s]+)\s*$", text, re.M | re.I)
        dates = re.findall(rf"^Invoice date\s+({ISO_DATE})\s+Due date\s+({ISO_DATE})\s*$", text, re.M | re.I)
        header = re.search(r"^No\.\s+AWB\s+Pickup\s+Description\s+Lane\s+Qty / basis\s+Rate\s+Amount \(([A-Z]{3})\)\s*$", text, re.M)
        if len(identity) != 1 or len(dates) != 1 or header is None:
            return None
        contracts = re.findall(r"^Contract reference\s+(\S+)\s*$", text, re.M | re.I)
        if len(contracts) > 1:
            return None
        currency = header[1]
        tail = text[header.end():]
        summary = re.search(rf"^Subtotal\s+{currency}\s+({NUMBER})\s*$", tail, re.M)
        if summary is None:
            return None
        rows = [line.strip() for line in tail[:summary.start()].splitlines() if line.strip()]
        pattern = re.compile(rf"(\d+)\s+(\S+)\s+({ISO_DATE})\s+(.+?)\s+(\S+)\s+({NUMBER})(?:\s+([A-Za-z]+))?\s+({NUMBER})(%)?\s+({NUMBER})")
        facts, amounts, row_ids = [], [], set()
        for row in rows:
            match = pattern.fullmatch(row)
            if match is None:
                return None
            index, shipment, raw_date, description, lane, qty, unit, rate, percent, amount = match.groups()
            if index in row_ids:
                return None
            row_ids.add(index)
            q, r, a = number(qty), number(rate), number(amount)
            expected = (q * r / (100 if percent else 1)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if expected != a:
                return None
            amounts.append(a)
            facts.append({**common, "kind": "invoice_line", "invoice_number": identity[0],
                          "line_number": index, "shipment_id": shipment, "service_date": observed(raw_date),
                          "description": description, "lane": lane, "quantity": str(q), "unit": unit,
                          "rate": str(r), "rate_unit": "percent" if percent else unit,
                          "amount": str(a), "currency": currency})
        footer = tail[summary.start():]
        tax = re.findall(rf"^Tax(?: not applicable)?\s+{currency}\s+({NUMBER})\s*$", footer, re.M)
        total = re.findall(rf"^Total\s+{currency}\s+({NUMBER})\s*$", footer, re.M)
        if not rows or len(tax) != 1 or len(total) != 1:
            return None
        subtotal, tax_amount, total_amount = number(summary[1]), number(tax[0]), number(total[0])
        if sum(amounts, Decimal(0)) != subtotal or subtotal + tax_amount != total_amount:
            return None
        # A late fee, monthly period, copy marker, or extra condition must not be
        # lost by accepting a simpler invoice grammar.
        if re.search(r"\b(?:late|overdue|credit|copy|duplicate|reminder|statement|waiv\w*|monthly|disputed|paid|override|ignore|assistant|automation)\b", text, re.I):
            return None
        invoice = {**common, "kind": "invoice", "invoice_number": identity[0],
                   "document_type": "invoice", "contract_id": contracts[0] if contracts else None,
                   "invoice_date": observed(dates[0][0]), "due_date": observed(dates[0][1]),
                   "currency": currency, "subtotal": str(subtotal), "tax": str(tax_amount), "total": str(total_amount)}
        customers = re.findall(r"^Bill to\s+(.+)$", text, re.M | re.I)
        if len(customers) != 1:
            return None
        invoice["customer"] = {"name": customers[0].strip()}
        issuer = text.splitlines()[0].strip()
        if not issuer or re.match(r"INVOICE\b", issuer, re.I):
            return None
        invoice["issuer"] = {"name": issuer}
        return [invoice, *facts]

    def _charges_advice(self, text: str, common: dict) -> list[dict] | None:
        identities = re.findall(r"^Charges advice\s+(\S+)\s*$", text, re.M | re.I)
        dates = re.findall(r"^Document date\s+(\d{2}/\d{2}/\d{4})\s+Payable by\s+(\d{2}/\d{2}/\d{4})\s*$", text, re.M | re.I)
        contract = re.findall(r"^Contract\s+(\S+)\s+Currency\s+([A-Z]{3})\s*$", text, re.M | re.I)
        customers = re.findall(r"^Customer\s+(.+)\s*$", text, re.M | re.I)
        header = re.search(r"^Route\s+Waybill\s+Charge\s+Basis\s+Rate\s+Amount\s+Collected\s*$", text, re.M | re.I)
        if not (len(identities) == len(dates) == len(contract) == len(customers) == 1) or header is None:
            return None
        currency = contract[0][1]
        tail = text[header.end():]
        subtotal_match = re.search(rf"^Charges\s+{currency}\s+({NUMBER})\s*$", tail, re.M)
        if subtotal_match is None:
            return None
        rows = [line.strip() for line in tail[:subtotal_match.start()].splitlines() if line.strip()]
        row_pattern = re.compile(
            rf"(\S+)\s+(\S+)\s+(.+?)\s+({NUMBER})(?:\s+([A-Za-z]+))?\s+({NUMBER})(%)?\s+({NUMBER})\s+(\d{{2}}/\d{{2}}/\d{{4}}|-)"
        )
        parsed = []
        all_dates = [dates[0][0], dates[0][1]]
        for row in rows:
            match = row_pattern.fullmatch(row)
            if match is None:
                return None
            parsed.append(match.groups())
            if match.group(9) != "-":
                all_dates.append(match.group(9))
        # Prefer an impossible month/day as proof. If all values are ambiguous,
        # the printed payment term can prove exactly one convention.
        day_first = None
        try:
            slash_date(all_dates[0], all_dates)
        except ValueError:
            terms = re.findall(r"^Terms\s+(\d+)\s+days\b", text, re.M | re.I)
            if len(terms) != 1:
                return None
            candidates = []
            for candidate in (True, False):
                try:
                    issued = date.fromisoformat(slash_date(dates[0][0], all_dates, day_first=candidate)["value"])
                    due = date.fromisoformat(slash_date(dates[0][1], all_dates, day_first=candidate)["value"])
                    if (due - issued).days == int(terms[0]):
                        candidates.append(candidate)
                except ValueError:
                    pass
            if len(candidates) != 1:
                return None
            day_first = candidates[0]
        facts, amounts = [], []
        for index, fields in enumerate(parsed, 1):
            lane, shipment, description, qty, unit, rate, percent, amount, collected = fields
            q, r, a = number(qty), number(rate), number(amount)
            late = re.fullmatch(rf"Late fee on (\S+): ([A-Z]{{3}}) ({NUMBER}) overdue, month (\d+) @ ({NUMBER})%", description, re.I)
            references = []
            basis = None
            if late:
                base = number(late[3])
                if r != number(late[5]) or currency != late[2]:
                    return None
                expected = (base * r / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                q, unit, basis = base, currency, f"overdue base; fee month {late[4]}"
                references = [{"entity_type": "invoice", "identifier": late[1],
                               "relation": "late fee charged on", "doc_ref": None}]
            else:
                expected = (q * r / (100 if percent else 1)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if expected != a:
                return None
            amounts.append(a)
            facts.append({**common, "kind": "invoice_line", "invoice_number": identities[0],
                          "line_number": str(index), "shipment_id": None if shipment == "-" else shipment,
                          "service_date": slash_date(collected, all_dates, day_first=day_first) if collected != "-" else None,
                          "description": description, "lane": None if lane == "-" else lane, "quantity": str(q), "unit": unit,
                          "rate": str(r), "rate_unit": "percent" if percent else unit,
                          "basis": basis, "amount": str(a), "currency": currency,
                          "references": references})
        footer = tail[subtotal_match.start():]
        taxes = re.findall(rf"^Tax(?:\s+\S+)?\s+{currency}\s+({NUMBER})\s*$", footer, re.M | re.I)
        totals = re.findall(rf"^Total payable\s+{currency}\s+({NUMBER})\s*$", footer, re.M | re.I)
        if not rows or len(taxes) != 1 or len(totals) != 1:
            return None
        subtotal, tax, total = number(subtotal_match[1]), number(taxes[0]), number(totals[0])
        if sum(amounts, Decimal(0)) != subtotal or subtotal + tax != total:
            return None
        if re.search(r"\b(?:credit|copy|duplicate|reminder|waiv\w*|disputed|override|ignore|assistant|automation)\b", text, re.I):
            return None
        issuer = text.splitlines()[0].strip()
        if not issuer:
            return None
        invoice = {**common, "kind": "invoice", "invoice_number": identities[0], "document_type": "invoice",
                   "contract_id": contract[0][0], "issuer": {"name": issuer}, "customer": {"name": customers[0].strip()},
                   "invoice_date": slash_date(dates[0][0], all_dates, day_first=day_first),
                   "due_date": slash_date(dates[0][1], all_dates, day_first=day_first),
                   "currency": currency, "subtotal": str(subtotal), "tax": str(tax), "total": str(total)}
        return [invoice, *facts]
