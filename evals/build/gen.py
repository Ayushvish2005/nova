"""Builds synthetic mailboxes for the eval cases: contracts, amendments, invoices in several layouts, tracking and AP
spreadsheets, and emails. Everything is made up: a carrier (Zephyrine Freight) and a customer (Ardent Biologics) that the
practice and exam mailboxes never show. Output is plain .eml files with PDF / XLSX attachments, laid out like the real
mailboxes (batch_01/0001.eml ...), so the harness reads them exactly as it reads ours.
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import openpyxl
import pymupdf

D = Decimal


def cents(x) -> Decimal:
    return D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


# ---- file builders ----------------------------------------------------------------------------------------------------
def pdf_bytes(lines: list[str], per_page: int = 58) -> bytes:
    """Text PDF: one text object per line, so reading order is the line order."""
    doc = pymupdf.open()
    for i in range(0, max(len(lines), 1), per_page):
        page = doc.new_page()
        y = 50
        for ln in lines[i:i + per_page]:
            page.insert_text((40, y), ln, fontsize=8)
            y += 12
    out = doc.tobytes()
    doc.close()
    return out


def pdf_pages(pages: list[list[str]]) -> bytes:
    """One PDF page per entry (so page numbers in evidence are predictable)."""
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 50
        for ln in lines:
            page.insert_text((40, y), ln, fontsize=8)
            y += 12
    out = doc.tobytes()
    doc.close()
    return out


def xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def eml(frm: tuple[str, str], to: str, when: str, subject: str, body: str, attachments=()) -> bytes:
    m = EmailMessage()
    m["From"] = f"{frm[0]} <{frm[1]}>"
    m["To"] = to
    m["Date"] = format_datetime(datetime.fromisoformat(when).replace(tzinfo=timezone.utc))
    m["Subject"] = subject
    m["Message-ID"] = f"<{hashlib.sha1((when + subject + body).encode()).hexdigest()[:16]}@eval.example>"
    m.set_content(body)
    for name, data in attachments:
        sub = "pdf" if name.endswith(".pdf") else "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        m.add_attachment(data, maintype="application", subtype=sub, filename=name)
    return m.as_bytes()


class Box:
    """A mailbox under construction: add emails to a batch, then write batch_01/0001.eml ..."""

    def __init__(self):
        self.batches: dict[str, list[bytes]] = {}

    def add(self, batch: str, frm: tuple[str, str], when: str, subject: str, body: str, attachments=(), to: str = None):
        self.batches.setdefault(batch, []).append(eml(frm, to or W.shipper_ap[1], when, subject, body, attachments))
        return f"{batch}/{len(self.batches[batch]):04d}.eml"

    def write(self, root: Path):
        for b, items in self.batches.items():
            d = root / b
            d.mkdir(parents=True, exist_ok=True)
            for i, raw in enumerate(items, 1):
                (d / f"{i:04d}.eml").write_bytes(raw)


# ---- the made-up world ------------------------------------------------------------------------------------------------
class W:
    cid = "ZFL-US-EXP-07"
    carrier, carrier_addr, carrier_party = "Zephyrine Freight Ltd.", "88 Quarry Lane, Suite 12, Brookfield 40210, USA", "ZFL-US"
    shipper, shipper_addr, shipper_party = "Ardent Biologics Inc.", "5 Alder Court, Suite 300, Millbrook 51733, USA", "ABI-US"
    effective, term_end, dated = "2040-01-10", "2041-01-09", "2040-01-05"
    p1 = ("2040-01-10", "2040-07-09")
    p2 = ("2040-07-10", "2041-01-09")
    people = [("Marta Vance", "Key Account Manager", "ZFL-US", "notices, billing_disputes", "marta.vance@zephyrine.example"),
              ("Orson Pike", "Pricing Manager", "ZFL-US", "rates", "orson.pike@zephyrine.example"),
              ("Dina Holt", "Customer Finance Lead", "ZFL-US", "waivers, payment_terms", "dina.holt@zephyrine.example"),
              ("Lev Quint", "Treasury Analyst", "ZFL-US", "treasury", "lev.quint@zephyrine.example"),
              ("Cora Mills", "AP Manager", "ABI-US", "notices, payment_terms, billing_disputes", "cora.mills@ardent.example"),
              ("Ben Tarn", "Logistics Director", "ABI-US", "rates", "ben.tarn@ardent.example")]
    marta = ("Marta Vance", "marta.vance@zephyrine.example")
    orson = ("Orson Pike", "orson.pike@zephyrine.example")
    dina = ("Dina Holt", "dina.holt@zephyrine.example")
    rex = ("Rex Ode", "rex.ode@zephyrine.example")           # operations: on no Schedule 1 category
    shipper_ap = ("Cora Mills", "cora.mills@ardent.example")
    billing = ("Zephyrine Billing", "billing@zephyrine.example")
    lanes = {"USA1-USB2": {"min": (80, 80), "bands": [(0, 44.5, 6.00, 5.90), (45, 99.5, 5.50, 5.40), (100, 299.5, 5.00, 4.90), (300, None, 4.50, 4.40)]},
             "USA1-USC3": {"min": (90, 90), "bands": [(0, 44.5, 7.00, 6.90), (45, 99.5, 6.50, 6.40), (100, 299.5, 6.00, 5.90), (300, None, 5.50, 5.40)]}}
    fuel = {"2040-01": "17.00", "2040-02": "17.50", "2040-03": "18.50", "2040-04": "19.00", "2040-05": "20.00", "2040-06": "20.50",
            "2040-07": "21.00", "2040-08": "21.00", "2040-09": "21.50", "2040-10": "22.00", "2040-11": "22.00", "2040-12": "21.00",
            "2041-01": "21.00"}
    lanes_inr = {"INA1-USB2": {"min": (6000, 6000), "bands": [(0, 44.5, 480, 470), (45, 99.5, 440, 430), (100, 299.5, 400, 390), (300, None, 360, 350)]}}
    fees_inr = {"DG": "10000.00", "DRYICE": "3000.00", "SEC": "8.00", "STOR": "2000.00", "DLV": "8000.00", "CUSTOMS": "16000.00",
                "ACCT": "24000.00", "SPROG": "48000.00", "PPROG": "32000.00", "PEAK": "24.00"}
    fees = {"DG": "130.00", "DRYICE": "40.00", "SEC": "0.10", "STOR": "25.00", "DLV": "100.00", "CUSTOMS": "200.00",
            "ACCT": "300.00", "SPROG": "600.00", "PPROG": "400.00", "PEAK": "0.30"}


def msa_lines(reworded: bool = False, cid: str = None, extra_people: list = None, term_end: str = None, inr_fx: dict = None) -> list[str]:
    """An MSA in the practice contracts' structure with this world's parties and numbers. `reworded` changes the late-fee and
    rebate clauses into wording the rule-based reader cannot parse, so the model fallback has to read them."""
    c = cid or W.cid
    te = term_end or W.term_end
    inr = inr_fx is not None
    lanes, fees, cur = (W.lanes_inr, W.fees_inr, "INR") if inr else (W.lanes, W.fees, "USD")
    if reworded:
        late = ("10.2 Late fee. A late payment charge of 2.00% accrues for each month, or part of a month, on undisputed sums that are "
                "more than 5 days overdue. days_late = paid date - due date; no charge if days_late <= 5; otherwise n = ceil(days_late / 30) months.")
        rebate = ("7.1 Volume rebate. For each Quarter the Carrier gives back a rebate on Qualifying Spend (QS) as follows: 5% once QS "
                  "reaches USD 50,000.00; 7.5% once QS reaches USD 100,000.00; 10% once QS reaches USD 150,000.00. The rate reached applies to all QS in the Quarter.")
    else:
        late = ("10.2 Late fee. Undisputed amounts unpaid more than 5 days after the due date bear a late fee of 2.00% per month or part "
                "thereof. days_late = paid date - due date; no fee is due if days_late <= 0; otherwise n = ceil(days_late / 30) fee months, each "
                "fee_i = 2.00% x base_i rounded to cents, base_i per clause 10.3.")
        rebate = ("7.1 Volume rebate. The Carrier shall credit a rebate on each Quarter's Qualifying Spend (QS) at the tier reached: QS >= USD "
                  "50,000.00: 5%; QS >= USD 100,000.00: 7.5%; QS >= USD 150,000.00: 10%. The tier rate applies to all QS in the Quarter.")
    head = f"Confidential | Master Services Agreement (Express Air) {c}"
    L = [head, "Master Services Agreement (Express Air)", f"Agreement No. {c}", "between", W.shipper, W.shipper_addr, "(the Shipper)", "and",
         W.carrier, W.carrier_addr, "(the Carrier)", f"Dated {W.dated}", "Initials: Shipper Carrier Page 1 of 4", "=====PAGE====="]
    body = [head,
            f"THIS AGREEMENT is dated {W.dated} and is made between:",
            f"(1) {W.shipper}, whose principal office is at {W.shipper_addr} (the Shipper);", "and",
            f"(2) {W.carrier}, whose principal office is at {W.carrier_addr} (the Carrier).",
            "NOW THEREFORE the parties agree as follows.",
            "1 Definitions. In this Agreement the following terms have the meanings below.",
            "1.1 Qualifying Spend. Qualifying Spend means freight charges (base freight, fuel, security surcharge and Annex D",
            "accessorials) on invoices dated in the period, net of all credit notes dated in the period whatever invoice they reference (rebate",
            "and improvement credits excepted). It excludes taxes (sales tax, GST), duties and disbursements, late fees, and rebate or",
            "improvement credits. INR amounts are converted per invoice at the Annex F rate for the month of the invoice date.",
            "1.2 Pickup Date. Pickup Date means the date the Carrier takes possession, as recorded in the Carrier's tracking record.",
            "1.3 Authorised Person. Authorised Person means a person listed in Schedule 1 for the relevant category.",
            "1.4 Contract Year. Contract Year means each 12-month period starting on the Effective Date or an anniversary of it.",
            "2 Term",
            f"2.1 Effective date. This Agreement takes effect on {W.effective} (the Effective Date).",
            f"2.2 Initial term. The initial term is 12 months, ending on {te}.",
            "2.3 Auto-renewal. This Agreement does not renew automatically. It ends at the end of the initial term unless extended under",
            "clause 2.4.",
            "2.4 Extension. The term may be extended only by an instrument signed by both parties.",
            "3 Rates and charges",
            "3.1 Rates. Services are charged at the rates in Annex A (express air). Period 2 rates continue to apply after month 12 of the term",
            "(including any renewal or extension) until an amendment replaces them.",
            "3.2 Pricing date. Each shipment is priced at the rates, surcharges and fees in force on its Pickup Date.",
            "3.3 Chargeable weight. Chargeable weight is the greater of actual and volumetric weight (L x W x H cm / 6000), rounded up to",
            "the next 0.5 kg.",
            "3.4 Minimum charge. The per-shipment minimum in Annex A applies where rate x chargeable weight is lower. Each shipment",
            "is rated at the weight band that contains its chargeable weight.",
            "3.5 Rate notices. The Carrier may change Annex A rates by written notice from an Authorised Person (rates), effective on the",
            "date stated in the notice.",
            "4 Surcharges",
            "4.1 Fuel surcharge. Fuel surcharge is the Annex C percentage for the month of the Pickup Date, applied to the base freight",
            "charge only. It does not apply to Annex D accessorials or the security surcharge.",
            "4.2 Security surcharge. Security surcharge is charged per chargeable kg at the Annex D rate.",
            "5 Dangerous goods and dry ice",
            "5.1 Dangerous goods fee. Shipments requiring a dangerous goods declaration attract the DG Fee in Annex D, once per",
            "shipment.",
            "5.2 Dry ice. Dry ice (UN1845) used solely as a refrigerant for non-dangerous goods is billed at the Dry Ice Handling Fee in",
            "Annex D, once per shipment (not per package or per kg), and does not attract the Dangerous Goods Fee.",
            "5.3 Mixed shipments. Where a shipment contains dangerous goods other than dry ice, only the DG Fee applies (not both).",
            "Initials: Shipper Carrier Page 2 of 4", "=====PAGE====="]
    p3 = [head,
          "6 Storage, delivery and customs",
          "6.1 Storage. Storage days = release date - arrival date (ARRIVED_FACILITY) - 3 free days, minimum 0. The arrival day",
          "counts; the release day (DELIVERED or COLLECTED_BY_CONSIGNEE) is not charged. Storage charge = storage days x the",
          "Annex D rate. Example: arrived 10 Mar, released 17 Mar, 3 free days: 17 - 10 - 3 = 4 days charged.",
          "6.2 Delivery. The delivery fee is payable only where the Carrier performs delivery to the consignee's address.",
          "6.3 Customs holds. Customs hold and examination fees are for the Shipper's account, except where the hold results from an",
          "error or omission in documents prepared or filed by the Carrier.",
          "7 Volume rebate", rebate,
          "7.2 Thresholds. Rebate thresholds are in USD per Quarter.",
          "7.3 Rebate credit. Rebate credit notes are issued within 45 days after Quarter end.",
          "8 Handling margin", "8.1 Handling margin. Not applicable to this Agreement.", "8.2 Margin step-down. Not applicable to this Agreement.",
          "9 Annual improvement credit",
          "9.1 Annual improvement credit. Within 60 days after each anniversary of the Effective Date, the Carrier shall issue a credit of",
          "1.00% of the prior Contract Year's Qualifying Spend.",
          "10 Payment", "10.1 Payment terms. Invoices are due 30 days after the invoice date.", late,
          "10.3 Compounding. Late fees unpaid at the end of a month are added to the overdue balance, and the next month's fee is",
          "charged on principal plus unpaid prior late fees. Fees shown paid leave the base.",
          "10.4 Disputed amounts. No late fee accrues on amounts disputed in good faith under clause 13 while the dispute is open.",
          "10.5 Extension of due date. A due date may be extended by written notice from the Carrier's Authorised Person (payment",
          "terms); the extended date replaces the due date for clause 10.2.",
          "11 Waivers",
          "11.1 Waiver authority. A charge may be waived only in writing by a person listed in Schedule 1 for waivers. Schedule 1 is",
          "exhaustive.",
          "11.2 Scope of waiver. A waiver applies only to the charges and shipments it names. No waiver is a waiver of any later charge,",
          "and no course of dealing creates one.",
          "11.3 Conditional waiver. A waiver stated to be conditional takes effect only when the condition is met.",
          "12 Notices and Schedule 1. Schedule 1 lists each party's Authorised Persons with categories (rates, waivers, payment_terms,",
          "notices [the Key Account Manager], billing_disputes, treasury) and email addresses. Notices are effective only from and to",
          "those addresses.",
          "13 Billing disputes",
          "13.1 Disputes. Billing disputes are raised with the Carrier's billing_disputes contact within 90 days of the invoice date.",
          "13.2 Dispute notice. A dispute notice names the invoice, the charge and the amount disputed. The undisputed balance of the",
          "invoice remains payable by its due date.",
          "13.3 Unranked instruments. Where a rate notice under clause 3.5 and an amendment state different rates for the same lane and",
          "period, the parties shall resolve the difference jointly; neither prevails automatically.",
          "14 Precedence",
          "14.1 Precedence. Signed amendment > this Agreement > Annexes. Between amendments, the later effective date prevails for",
          "the terms it changes.",
          "14.2 Correspondence. This clause does not rank correspondence against amendments (see clause 13.3).",
          "14.3 Quoted text. Quoted or forwarded text is not a notice.",
          "Initials: Shipper Carrier Page 3 of 4", "=====PAGE====="]
    ann = [head, f"Annex A: Express rates ({cur} per chargeable kg; minimum charge per shipment)",
           f"Period P1: pickups {W.p1[0]} to {W.p1[1]}", f"Period P2: pickups {W.p2[0]} to {W.p2[1]}", "Lane Weight band P1 rate P2 rate"]
    for lane, spec in lanes.items():
        ann.append(f"{lane} minimum charge {spec['min'][0]:.2f} {spec['min'][1]:.2f}")
        for lo, hi, r1, r2 in spec["bands"]:
            band = f"{lo:g}-{hi:g} kg" if hi is not None else f"{lo:g}+ kg"
            ann.append(f"{lane} {band} {r1:.2f} {r2:.2f}")
    ann += ["Annex B: Not applicable to this Agreement.", "=====PAGE====="]
    fuel = [head, "Annex C: Fuel surcharge by month of Pickup Date (percentage of base freight)", "Month Fuel surcharge"]
    fuel += [f"{m} {p}%" for m, p in W.fuel.items()]
    fuel += ["=====PAGE====="]
    d = [head, "Annex D: Accessorials", "Code Accessorial Fee Basis",
         f"DG Dangerous goods fee {fees['DG']} {cur} per shipment", f"DRYICE Dry ice handling fee {fees['DRYICE']} {cur} per shipment",
         f"SEC Security surcharge {fees['SEC']} {cur} per chargeable kg", f"STOR Storage {fees['STOR']} {cur} per storage day",
         f"DLV Delivery fee {fees['DLV']} {cur} per shipment", f"CUSTOMS Customs hold fee {fees['CUSTOMS']} {cur} per shipment",
         f"ACCT Monthly account fee {fees['ACCT']} {cur} per month", f"SPROG Storage programme fee {fees['SPROG']} {cur} per month",
         f"PPROG Pickup programme fee {fees['PPROG']} {cur} per month",
         f"PEAK Peak season surcharge {fees['PEAK']} {cur} per chargeable kg, for pickups in", "September to November only",
         "Annex E: Charge codes (the Carrier may print any listed label for the code)",
         "Code FRT: Air freight; also billed as: Express freight, Freight charge", "Code FUEL: Fuel surcharge; also billed as: FSC, Fuel adj.",
         "Code SEC: Security surcharge; also billed as: Screening fee, Screening fee (adj.), Security screening",
         "Code DG: Dangerous goods fee; also billed as: DG handling, Hazmat handling",
         "Code DRYICE: Dry ice handling fee; also billed as: Dry ice handling",
         "Code STOR: Storage; also billed as: Warehouse storage, Demurrage",
         "Code DLV: Delivery fee; also billed as: Door delivery, Final-mile delivery",
         "Code CUSTOMS: Customs hold fee; also billed as: Customs examination, Hold handling",
         "Code ACCT: Monthly account fee; also billed as: Account maintenance",
         "Code SPROG: Storage programme fee; also billed as: Storage programme",
         "Code PPROG: Pickup programme fee; also billed as: Pickup programme", "Code PEAK: Peak season surcharge; also billed as: PSS",
         "Code COST: Carrier cost; also billed as: Linehaul cost pass-through", "Code MARGIN: Handling margin; also billed as: Forwarding margin",
         "Code HDL: Special handling; also billed as: Supplementary handling",
         "Code DUTY: Import duty (disbursement); also billed as: Duty paid on your behalf", "Code TAX: Sales tax; also billed as: GST",
         "Code LATE: Late payment fee; also billed as: Late fee, Overdue interest", "=====PAGE====="]
    f = [head, "Annex F: Exchange rates (INR per 1 USD)"] + (["Month INR per 1 USD"] + [f"{m} {r}" for m, r in inr_fx.items()] if inr else
         ["Not applicable: all charges under this Agreement are in USD."]) + [
         "Schedule 1: Authorised Persons (name, title, party: categories, notice address)"]
    for n, t, p, cats, em in W.people + (extra_people or []):
        f.append(f"{n}, {t}, {p}: {cats} <{em}>")
    f += ["Initials: Shipper Carrier Page 4 of 4"]
    pages = [L, body, p3, ann, fuel, d, f]
    flat: list[str] = []
    for p in pages:
        flat += [x for x in p if x != "=====PAGE====="] + ["=====PAGE====="]
    return flat


def pages_of(lines: list[str]) -> list[list[str]]:
    out, cur = [], []
    for ln in lines:
        if ln == "=====PAGE=====":
            out.append(cur)
            cur = []
        else:
            cur.append(ln)
    return out + ([cur] if cur else [])


def msa_pdf(**kw) -> bytes:
    return pdf_pages(pages_of(msa_lines(**kw)))


def amendment_pdf(no: int, effective: str, lane: str = None, rows: list[tuple] = None, cited: tuple = None,
                  extend_to: str = None, cid: str = None) -> bytes:
    """Amendment No. `no`: replaces a lane's rates with `rows` [(band label, rate)], and/or cites an annex revision, or extends the term."""
    c = cid or W.cid
    L = [f"Confidential | Amendment No. {no} to {c}", f"Amendment No. {no} to Master Services Agreement (Express Air) {c}",
         f"Amendment reference {c}/AMD-{no}", f"Effective from {effective}",
         f"This Amendment is made between {W.shipper} (the Shipper) and {W.carrier} (the Carrier).", "Recitals",
         f"(A) The parties entered into the Master Services Agreement (Express Air) {c} dated {W.dated} (the Agreement).",
         "(B) The parties wish to amend the Agreement as set out below. Words defined in the Agreement have the same meaning in this",
         "Amendment.", "The parties agree as follows:"]
    if rows:
        L += ["1. Annex A rates for the lanes below are deleted and replaced with the following, for every period, with effect from the effective",
              "date:", "Lane Weight band Rate Currency"]
        L += [f"{lane} {band} {rate:.2f} USD" for band, rate in rows]
    if cited:
        L += [f"2. The Annex A rates for lane(s) {cited[1]} are deleted and replaced as follows: {cited[0]} (attached) applies from {cited[2]} to lane(s) {cited[1]}."]
    if extend_to:
        L += [f"3. The term of the Agreement is extended and now ends on {extend_to}."]
    L += ["All other terms of the Agreement are unchanged.", "This Amendment may be executed in counterparts, including by electronic signature.",
          "Executed by duly authorised officers. Schedule 1 governs Authorised Persons for rates, waivers, payment terms and notices.",
          f"Signed for and on behalf of {W.shipper} (the Shipper)", "Signature:", "Name: Hal Brandt", "Title: VP Procurement", f"Date: {effective}",
          f"Signed for and on behalf of {W.carrier} (the Carrier)", "Signature:", "Name: Ines Calder", "Title: VP Sales", f"Date: {effective}",
          "Initials: Shipper Carrier Page 1 of 1"]
    return pdf_bytes(L)


# ---- invoices ---------------------------------------------------------------------------------------------------------
def freight_lines(awb, pickup, lane, kg, rate, fuel_pct, extra=(), label_frt="Air freight", label_fuel="Fuel surcharge",
                  sec=True, delivery=True, min_charge=None, sec_rate="0.10", dlv="100.00"):
    """Lines for one shipment, arithmetic correct as billed: [(desc, qty, unit, rate, amount)]; extras are ready-made tuples."""
    frt = cents(D(str(rate)) * D(str(kg)))
    if min_charge is not None and frt < D(str(min_charge)):
        frt = D(str(min_charge))
    L = [(label_frt, str(kg), "kg", f"{rate:.2f}", frt), (label_fuel, f"{frt}", None, f"{fuel_pct}%", cents(frt * D(str(fuel_pct)) / 100))]
    if sec:
        L.append(("Security surcharge", str(kg), "kg", sec_rate, cents(D(str(kg)) * D(sec_rate))))
    if delivery:
        L.append(("Delivery fee", "1", "shpt", dlv, D(dlv)))
    L += list(extra)
    return [(awb, pickup, lane) + x for x in L]


def invoice_pdf(layout: str, number: str, date: str, due: str, lines: list[tuple], contract: str = None, copy_marker: str = None,
                late_lines: list[tuple] = None, currency: str = "USD") -> bytes:
    """lines: (awb, date, lane, desc, qty, unit, rate, amount). Layouts: classic, swapped, advice, freeform."""
    c = contract or W.cid
    cur = currency
    total = sum((l[7] for l in lines), D(0))
    extra_total = D(0)
    late_lines = late_lines or []
    for _text, amt in late_lines:
        extra_total += D(str(amt))
    total += extra_total
    L = [W.carrier, W.carrier_addr]
    mk = [f"** {copy_marker} **"] if copy_marker else []
    qtyfmt = lambda l: f"{l[4]} {l[5]}" if l[5] else f"{l[4]}"
    if layout == "classic":
        L += [f"INVOICE {number}", f"Invoice date {date} Due date {due}", f"Contract reference {c}", f"Bill to {W.shipper}", W.shipper_addr] + mk
        L += [f"No. AWB Pickup Description Lane Qty / basis Rate Amount ({cur})"]
        n = 0
        for l in lines:
            n += 1
            L.append(f"{n} {l[0]} {l[1]} {l[3]} {l[2]} {qtyfmt(l)} {l[6]} {l[7]}")
        for s, _amt in late_lines:
            n += 1
            L.append(f"{n} - - {s}")
        L += [f"Subtotal {cur} {total}", f"Tax not applicable {cur} 0.00", f"Total {cur} {total}", "Payment terms: net 30 days."]
    elif layout == "swapped":                       # amount before units and unit price, different labels
        L += [f"Freight bill no. {number}", f"Issued on {_fmt_d(date)} Settle by {_fmt_d(due)}", f"Service agreement {c}", f"Account {W.shipper}"] + mk
        L += [f"Charge Shipment Route Collected Net ({cur}) Units Unit price"]
        for l in lines:
            pct = str(l[6]).endswith("%")
            units = f"{l[4]} {l[5]}" if l[5] else (f"{l[4]}" if not pct else f"{l[4]}")
            L.append(f"{l[3]} {l[0]} {l[2]} {_fmt_d(l[1])} {l[7]} {units} {l[6]}")
        for s, _amt in late_lines:
            L.append(f"- - {s}")
        L += [f"Net charges {cur} {total}", f"Tax none charged {cur} 0.00", f"Balance due {cur} {total}", "Settlement 30 days net."]
    elif layout == "advice":                        # day-first dates, route first, collected date last
        L += [f"Charges advice {number}", f"Document date {_slash(date)} Payable by {_slash(due)}", f"Contract {c} Currency {cur}", f"Customer {W.shipper}"] + mk
        L += ["Route Waybill Charge Basis Rate Amount Collected"]
        for l in lines:
            L.append(f"{l[2]} {l[0]} {l[3]} {qtyfmt(l)} {l[6]} {l[7]} {_slash(l[1])}")
        for s, _amt in late_lines:
            L.append(f"- - {s} -")
        L += [f"Charges {cur} {total}", f"Tax nil {cur} 0.00", f"Total payable {cur} {total}", "Terms 30 days."]
    else:
        raise ValueError(layout)
    return pdf_bytes(L)


def _fmt_d(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(d):02d} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m)-1]} {y}"


def _slash(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def freeform_invoice_pdf(number: str, date: str, due: str, lines: list[tuple], contract: str = None) -> bytes:
    """A layout no table parser would read: prose-like lines. Forces the model fallback."""
    c = contract or W.cid
    total = sum((l[7] for l in lines), D(0))
    L = [f"{W.carrier} - charges for {W.shipper}", f"Doc ref: {number}   Issued: {_fmt_d(date)}   Terms: pay within 30 days (due {_fmt_d(due)})",
         f"Agreement: {c}   Currency: USD"]
    cur = None
    for l in lines:
        if l[0] != cur:
            cur = l[0]
            L.append(f"Shipment {l[0]} collected {l[1]}, lane {l[2]}")
        if str(l[6]).endswith("%"):
            L.append(f"  {l[3]} ...... {l[6]} of {l[4]} = {l[7]}")
        elif l[5] == "kg":
            L.append(f"  {l[3]} ...... {l[4]} kg @ {l[6]} = {l[7]}")
        else:
            L.append(f"  {l[3]} ...... {l[7]}")
    L.append(f"Total due: {total}")
    return pdf_bytes(L)


def statement_xlsx(number: str, date: str, due: str, rows: list[tuple], contract: str = None) -> bytes:
    c = contract or W.cid
    total = sum((r[4] for r in rows), D(0))
    data = [[W.carrier], [f"STATEMENT OF ACCOUNT {number}"], [f"Statement date {date}", f"Due date {due}"], [f"Bill to {W.shipper}"],
            [f"Contract reference {c}"], ["Charge", "Reference", "Quantity", "Rate", "Amount"]]
    data += [[r[0], r[1], r[2], r[3], str(r[4])] for r in rows]
    data += [["Total USD", str(total)]]
    return xlsx_bytes(data)


def tracking_xlsx(events: list[tuple]) -> bytes:
    return xlsx_bytes([["AWB", "Event", "Date", "Station", "Reason code"]] + [[a, e, d, s, "-"] for a, e, d, s in events])


def ap_xlsx(rows: list[tuple]) -> bytes:
    """rows: (invoice, invoice_date, due, amount, status, paid_date or '-')."""
    return xlsx_bytes([["Invoice no.", "Invoice date", "Due date", "Amount", "Status", "Paid date"]] +
                      [[r[0], r[1], f"due {r[2]}", f"USD {r[3]}", r[4], r[5]] for r in rows])


def awb_pdf(awb: str, date: str, origin: str, dest: str, kg: str, goods: str = "diagnostic kits", handling: str = None) -> bytes:
    L = [W.carrier, f"AIR WAYBILL {awb}", f"Executed on {date} at {origin}", f"Origin {origin} | Destination {dest}", "Pieces 1",
         f"Actual weight {kg} kg", "Dimensions 40x40x40 cm x 1", f"Chargeable weight {kg} kg", f"Nature of goods: {goods}"]
    if handling:
        L.append(f"Handling information: {handling}")
    return pdf_bytes(L + ["Page 1 of 1"])


def dgd_pdf(awb: str, items: list[str], goods: str = "reagent kits") -> bytes:
    return pdf_bytes([W.shipper, f"SHIPPER'S DECLARATION FOR AWB {awb}", f"Goods: {goods}"] + items + ["Page 1 of 1"])


def case_yaml(name: str, mailbox: str, as_of: str, expect: dict, why: str, mode: str = "supervised", feedback: dict = None) -> str:
    import yaml
    d = {"name": name, "mailbox": mailbox, "as_of": as_of, "mode": mode}
    if feedback:
        d["feedback"] = feedback
    d["expect"] = expect
    d["why"] = why
    return yaml.safe_dump(d, sort_keys=False, allow_unicode=True)
