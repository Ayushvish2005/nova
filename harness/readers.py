"""Read MIME documents using the grader's text layer and a scan fallback."""
from __future__ import annotations

from datetime import timezone
from email.utils import getaddresses, parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from importlib.metadata import version
from pathlib import Path

from grader import extract as grader_extract

from .models import Extraction, Page, Source
from .ocr import OCREngine, RapidOCREngine, reading_order

SUPPORTED_FORMATS = {"eml", "pdf", "xlsx"}
MIME_FORMATS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "message/rfc822": "eml",
}


def message_sources(batch_dir: Path, path: Path) -> list[tuple[Source, bytes]]:
    """One receipt plus its attachments; source paths never depend on the workspace."""
    data = path.read_bytes()
    msg = grader_extract.parse_eml(data)
    doc_ref = f"{batch_dir.name}/{path.relative_to(batch_dir).as_posix()}"
    if "#" in doc_ref:
        raise ValueError(f"{doc_ref}: '#' in an email path makes evidence ambiguous")
    try:
        received = parsedate_to_datetime(str(msg["Date"]))
        if received.tzinfo is None:
            raise ValueError("Date header has no timezone")
        received_at = received.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{doc_ref}: invalid email Date header: {msg['Date']!r}") from exc

    metadata = {
        "message_id": str(msg.get("Message-ID", "")),
        "subject": str(msg.get("Subject", "")),
        "date_header": str(msg["Date"]),
        "in_reply_to": str(msg.get("In-Reply-To", "")),
        "references": str(msg.get("References", "")),
    }
    for header in ("From", "To", "Cc"):
        metadata[header.lower()] = [
            {"name": name, "address": address}
            for name, address in getaddresses([str(v) for v in msg.get_all(header, [])])
        ]
    sources = [(Source(
        doc_ref=doc_ref, batch=batch_dir.name, kind="email", content_hash=sha256(data).hexdigest(),
        format="eml", media_type="message/rfc822", received_at=received_at, metadata=metadata,
    ), data)]
    seen: set[str] = set()
    for index, part in enumerate(msg.iter_attachments(), 1):
        filename = part.get_filename()
        if not filename or filename in seen:
            raise ValueError(f"{doc_ref}: attachment {index} has a missing or duplicate filename; cannot cite it uniquely")
        seen.add(filename)
        payload = part.get_payload(decode=True)
        if payload is None and part.get_content_type() == "message/rfc822":
            payload = part.get_payload(0).as_bytes()
        if payload is None:
            raise ValueError(f"{doc_ref}#{filename}: cannot decode attachment bytes")
        suffix = Path(filename).suffix.lower().lstrip(".")
        file_format = suffix if suffix in SUPPORTED_FORMATS else MIME_FORMATS.get(part.get_content_type(), suffix or "unknown")
        sources.append((Source(
            doc_ref=f"{doc_ref}#{filename}", batch=batch_dir.name, kind="attachment",
            content_hash=sha256(payload).hexdigest(), format=file_format, media_type=part.get_content_type(),
            received_at=received_at, metadata={}, parent_ref=doc_ref, filename=filename, attachment_index=index,
        ), payload))
    return sources


class _HTMLText(HTMLParser):
    """Readable HTML-only body; original grader text is retained separately."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


class DocumentReader:
    def __init__(self, ocr: OCREngine | None = None, dpi: int = 160) -> None:
        if dpi <= 0:
            raise ValueError("OCR render DPI must be positive")
        self.ocr = ocr if ocr is not None else RapidOCREngine()
        self.dpi = dpi

    @property
    def cache_key(self) -> str:
        return (f"reader:1;pymupdf:{version('pymupdf')};openpyxl:{version('openpyxl')};"
                f"{self.ocr.cache_key};dpi:{self.dpi}")

    def extract(self, data: bytes, file_format: str) -> Extraction:
        if file_format not in SUPPORTED_FORMATS:
            return Extraction("unsupported", error=f"unsupported document format: {file_format}")
        # Exceptions are recorded by ingestion. Do not silently treat failed OCR as an empty page.
        native_pages = grader_extract.pages_of(data, file_format)
        if not native_pages:
            raise ValueError("document has no pages")
        if file_format == "pdf":
            return Extraction("ready", self._pdf_pages(data, native_pages))
        pages = [Page(i, text, text) for i, text in enumerate(native_pages, 1)]
        if file_format == "eml":
            msg = grader_extract.parse_eml(data)
            if msg.get_body(preferencelist=("plain",)) is None:
                body = msg.get_body(preferencelist=("html",))
                if body:
                    parser = _HTMLText()
                    parser.feed(body.get_content())
                    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
                    pages = [Page(1, native_pages[0] + "\n" + text, native_pages[0], method="html")]
        return Extraction("ready", pages)

    def _pdf_pages(self, data: bytes, native_pages: list[str]) -> list[Page]:
        import pymupdf

        pages = []
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            for number, (page, native) in enumerate(zip(pdf, native_pages), 1):
                images = page.get_image_info()
                area = max(1.0, page.rect.get_area())
                image_heavy = any((pymupdf.Rect(im["bbox"]) & page.rect).get_area() / area >= 0.5 for im in images)
                needs_ocr = image_heavy or (not native.strip() and bool(images or page.get_drawings()))
                if not needs_ocr:
                    pages.append(Page(number, native, native))
                    continue
                pix = page.get_pixmap(dpi=self.dpi, colorspace=pymupdf.csRGB, alpha=False)
                text, regions = reading_order(self.ocr.recognize(pix.tobytes("png")))
                if not text.strip():
                    raise ValueError(f"OCR produced no text on image/vector page {number}")
                pages.append(Page(number, text, native, "ocr", regions, pix.width, pix.height))
        return pages
