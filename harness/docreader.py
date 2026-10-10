"""Read a mailbox batch into documents, page by page, exactly as the grader's text layer does (grader/extract.py), so
quotes taken from here are found where we say. Standalone: the harness must not import from grader/.

A document is an email (one page: header lines + plain body) or one of its PDF / XLSX attachments.
doc ids follow the schema: 'batch_01/0014.eml' and 'batch_01/0014.eml#INV-1.pdf'.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

SEP = " | "


def norm(s: str) -> str:
    return " ".join(s.split())


# ---- PDF --------------------------------------------------------------------------------------------------------------
def _reading_order(page) -> str:
    words = page.get_text("words")
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in words:
        mid = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - mid) <= 2.5:
            lines[-1][1].append(w)
        else:
            lines.append([mid, [w]])
    return "\n".join(" ".join(x[4] for x in sorted(ws, key=lambda w: w[0])) for _, ws in lines)


def pdf_pages(data: bytes) -> list[str]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [_reading_order(p) for p in doc]


def pdf_is_image_only(data: bytes) -> bool:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return all(not p.get_text().strip() for p in doc)


def render_pdf_page_png(data: bytes, page: int, zoom: float = 2.0) -> bytes:
    """One PDF page (1-based) as PNG bytes, for a vision model. Used for scans."""
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


_ocr_engine = None


def ocr_pages(data: bytes) -> list[str]:
    """Scanned PDF -> text lines per page, with the local OCR (rapidocr). It returns boxes, not lines, and drops most
    spaces inside a box ('Airfreight', '106kg'): boxes are grouped into lines by position and the extractor is told."""
    global _ocr_engine
    import pymupdf
    from rapidocr_onnxruntime import RapidOCR
    _ocr_engine = _ocr_engine or RapidOCR()
    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for i in range(len(doc)):
            res, _ = _ocr_engine(render_pdf_page_png(data, i + 1, zoom=3.0))
            boxes = sorted((((min(p[1] for p in b) + max(p[1] for p in b)) / 2,
                             max(p[1] for p in b) - min(p[1] for p in b), min(p[0] for p in b), t)
                            for b, t, _c in (res or [])))
            lines: list[list] = []
            for y, h, x, t in boxes:
                if lines and abs(lines[-1][0] - y) <= max(8, h * 0.5):
                    lines[-1][1].append((x, t))
                else:
                    lines.append([y, [(x, t)]])
            pages.append("\n".join(" ".join(t for _, t in sorted(ws)) for _, ws in lines))
    return pages


# ---- XLSX -------------------------------------------------------------------------------------------------------------
def _cell_text(cell) -> str:
    v = cell.value
    if v is None:
        return ""
    fmt = cell.number_format or "General"
    prefix = ""
    m = re.match(r'^"([^"]*)"(.*)$', fmt)
    if m:
        prefix, fmt = m.group(1), m.group(2)
    if isinstance(v, datetime):
        return prefix + v.date().isoformat()
    if isinstance(v, date):
        return prefix + v.isoformat()
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        dp = len(fmt.split(".")[1]) if "." in fmt else 0
        return prefix + f"{Decimal(str(v)):.{dp}f}"
    return prefix + str(v)


def xlsx_pages(data: bytes) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    pages = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows():
            cells = [t for t in (_cell_text(c) for c in row) if t != ""]
            if cells:
                rows.append(SEP.join(cells))
        pages.append("\n".join(rows))
    return pages


# ---- email ------------------------------------------------------------------------------------------------------------
def parse_eml(data: bytes):
    return BytesParser(policy=policy.default).parsebytes(data)


def received_utc(msg) -> datetime:
    return parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc)


def email_lines(msg) -> list[str]:
    name, addr = parseaddr(str(msg["From"]))
    out = [f"From: {name} <{addr}>", f"To: {msg['To']}"]
    if msg["Cc"]:
        out.append(f"Cc: {msg['Cc']}")
    dt = parsedate_to_datetime(str(msg["Date"]))
    out += [f"Date: {dt.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"Subject: {msg['Subject']}", ""]
    body = msg.get_body(preferencelist=("plain",))
    out += body.get_content().replace("\r\n", "\n").rstrip("\n").split("\n") if body else []
    names = [p.get_filename() for p in msg.iter_attachments()]
    if names:
        out.append("Attachments: " + ", ".join(names))
    return out


def pages_of(data: bytes, ext: str) -> list[str]:
    ext = ext.lower().lstrip(".")
    if ext == "pdf":
        return pdf_pages(data)
    if ext == "xlsx":
        return xlsx_pages(data)
    raise ValueError(f"can't read .{ext} files")


# ---- numbered lines: extractors return line numbers, we copy the text (quotes are never model-written) -----------------
def numbered(page_text: str) -> str:
    return "\n".join(f"L{i}: {ln}" for i, ln in enumerate(page_text.split("\n"), 1))


def pick_lines(page_text: str, nums: list[int]) -> str:
    """The exact text of the given 1-based line numbers, joined with a space (a clause wrapped over lines)."""
    lines = page_text.split("\n")
    return " ".join(lines[n - 1].strip() for n in sorted(set(nums)) if 1 <= n <= len(lines))


# ---- one batch --------------------------------------------------------------------------------------------------------
def load_batch(batch_dir: str | Path) -> list[dict]:
    """Every .eml in the batch folder, in name order -> list of document dicts (the email, then its attachments).

    A document: {doc_id, parent, kind: email|pdf|xlsx, name, received_at, pages: [str], is_scan, sha, bytes?}
    Email docs also carry from_name, from_addr, to, cc, subject, message_id. Attachment bytes are kept under
    'bytes' so a scan can be rendered; the caller drops them before saving.
    """
    batch = Path(batch_dir)
    docs: list[dict] = []
    for f in sorted(batch.glob("*.eml")):
        raw = f.read_bytes()
        msg = parse_eml(raw)
        eid = f"{batch.name}/{f.name}"
        rcv = received_utc(msg).strftime("%Y-%m-%dT%H:%M:%SZ")
        name, addr = parseaddr(str(msg["From"]))
        docs.append({
            "doc_id": eid, "parent": None, "kind": "email", "name": f.name, "received_at": rcv,
            "from_name": name, "from_addr": addr.lower(), "to": str(msg["To"] or ""), "cc": str(msg["Cc"] or ""),
            "subject": str(msg["Subject"] or ""), "message_id": str(msg["Message-ID"] or ""),
            "pages": ["\n".join(email_lines(msg))], "is_scan": False,
            "sha": hashlib.sha256(raw).hexdigest(),
        })
        for p in msg.iter_attachments():
            aname = p.get_filename() or ""
            ext = aname.rsplit(".", 1)[-1].lower() if "." in aname else ""
            data = p.get_content()
            if ext not in ("pdf", "xlsx") or not isinstance(data, (bytes, bytearray)):
                continue            # ponytail: other attachment types are ignored; add a reader if a mailbox has them
            docs.append({
                "doc_id": f"{eid}#{aname}", "parent": eid, "kind": ext, "name": aname, "received_at": rcv,
                "pages": pages_of(bytes(data), ext), "is_scan": ext == "pdf" and pdf_is_image_only(bytes(data)),
                "sha": hashlib.sha256(bytes(data)).hexdigest(), "bytes": bytes(data),
            })
    return docs
