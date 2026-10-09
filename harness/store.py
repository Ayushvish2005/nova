"""Transactional SQLite storage for immutable source occurrences and cached pages."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator

from .models import Document, Extraction, OCRRegion, Page, Source

SCHEMA_VERSION = 3


class SourceConflict(ValueError):
    """A citation path was reused for different source material."""


class StateStore:
    def __init__(self, state_dir: str | Path) -> None:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "state.sqlite3"
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            current = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if current == 0:
                schema = (Path(__file__).with_name("schema") / "state.sql").read_text()
                self.connection.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
                current = 1
            if current == 1:
                migration = (Path(__file__).with_name("schema") / "002_facts.sql").read_text()
                self.connection.executescript("BEGIN IMMEDIATE;\n" + migration + "\nCOMMIT;")
                current = 2
            if current == 2:
                migration = (Path(__file__).with_name("schema") / "003_reporting.sql").read_text()
                self.connection.executescript("BEGIN IMMEDIATE;\n" + migration + "\nCOMMIT;")
                current = 3
            if current != SCHEMA_VERSION:
                raise ValueError(f"unsupported state schema {current}; expected {SCHEMA_VERSION}")
        except Exception:
            self.connection.close()
            raise

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *args) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One batch is committed atomically, including recorded extraction failures."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def get_source(self, doc_ref: str) -> Source | None:
        row = self.connection.execute("SELECT * FROM documents WHERE doc_ref = ?", (doc_ref,)).fetchone()
        if row is None:
            return None
        return Source(
            doc_ref=row["doc_ref"], batch=row["batch"], kind=row["kind"], content_hash=row["content_hash"],
            format=row["format"], media_type=row["media_type"], received_at=row["received_at"],
            metadata=json.loads(row["metadata_json"]), parent_ref=row["parent_ref"], filename=row["filename"],
            attachment_index=row["attachment_index"],
        )

    def check_source(self, source: Source) -> bool:
        """Return whether the exact receipt exists; never replace evidence in place."""
        previous = self.get_source(source.doc_ref)
        if previous is not None and previous != source:
            raise SourceConflict(f"{source.doc_ref}: source changed at an existing evidence reference")
        return previous is not None

    def cached_extraction(self, content_hash: str, file_format: str, reader_version: str) -> Extraction | None:
        key = (content_hash, file_format, reader_version)
        row = self.connection.execute(
            "SELECT status, error FROM extractions WHERE content_hash = ? AND format = ? AND reader_version = ?", key,
        ).fetchone()
        if row is None:
            return None
        pages = []
        for p in self.connection.execute(
            "SELECT * FROM pages WHERE content_hash = ? AND format = ? AND reader_version = ? ORDER BY number", key,
        ):
            pages.append(Page(
                number=p["number"], text=p["text"], native_text=p["native_text"], method=p["method"],
                regions=[OCRRegion(**region) for region in json.loads(p["regions_json"])],
                width=p["width"], height=p["height"],
            ))
        return Extraction(row["status"], pages, row["error"])

    def save(self, document: Document, data: bytes, *, extracted: bool) -> bool:
        """Persist a validated document inside transaction(); return True for a new source."""
        if not self.connection.in_transaction:
            raise RuntimeError("document writes require a transaction")
        document.validate()
        source = document.source
        if sha256(data).hexdigest() != source.content_hash:
            raise ValueError(f"{source.doc_ref}: content hash does not match original bytes")
        exists = self.check_source(source)
        self.connection.execute(
            "INSERT OR IGNORE INTO batches(name, first_ingested_at) VALUES (?, ?)",
            (source.batch, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self.connection.execute("INSERT OR IGNORE INTO blobs(content_hash, data) VALUES (?, ?)", (source.content_hash, data))
        cache_key = (source.content_hash, source.format, document.reader_version)
        if extracted:
            self.connection.execute(
                """INSERT INTO extractions(content_hash, format, reader_version, status, error) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_hash, format, reader_version) DO UPDATE SET
                    status = excluded.status, error = excluded.error, attempts = attempts + 1""",
                (*cache_key, document.extraction.status, document.extraction.error),
            )
            self.connection.execute(
                "DELETE FROM pages WHERE content_hash = ? AND format = ? AND reader_version = ?", cache_key,
            )
            for p in document.extraction.pages:
                self.connection.execute(
                    """INSERT INTO pages(content_hash, format, reader_version, number, text, native_text,
                       method, regions_json, width, height) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*cache_key, p.number, p.text, p.native_text, p.method,
                     json.dumps([asdict(region) for region in p.regions], allow_nan=False), p.width, p.height),
                )
        if not exists:
            self.connection.execute(
                """INSERT INTO documents(doc_ref, batch, kind, content_hash, format, media_type, received_at,
                   metadata_json, parent_ref, filename, attachment_index, reader_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source.doc_ref, source.batch, source.kind, source.content_hash, source.format, source.media_type,
                 source.received_at, json.dumps(source.metadata, ensure_ascii=False), source.parent_ref,
                 source.filename, source.attachment_index, document.reader_version),
            )
        else:
            self.connection.execute(
                "UPDATE documents SET reader_version = ? WHERE doc_ref = ? AND reader_version != ?",
                (document.reader_version, source.doc_ref, document.reader_version),
            )
        return not exists

    def get_document(self, doc_ref: str) -> Document | None:
        source = self.get_source(doc_ref)
        if source is None:
            return None
        reader_version = self.connection.execute(
            "SELECT reader_version FROM documents WHERE doc_ref = ?", (doc_ref,),
        ).fetchone()[0]
        extraction = self.cached_extraction(source.content_hash, source.format, reader_version)
        if extraction is None:
            raise RuntimeError(f"{doc_ref}: missing extraction record")
        return Document(source, reader_version, extraction)

    def documents(self, *, as_of: str | None = None) -> Iterator[Document]:
        """Read source occurrences in stable order, optionally through a UTC report date."""
        query = "SELECT doc_ref FROM documents"
        parameters: tuple = ()
        if as_of is not None:
            cutoff = date.fromisoformat(as_of) + timedelta(days=1)
            query += " WHERE received_at < ?"
            parameters = (f"{cutoff.isoformat()}T00:00:00Z",)
        query += " ORDER BY doc_ref"
        for row in self.connection.execute(query, parameters):
            document = self.get_document(row[0])
            if document is not None:
                yield document

    def raw_bytes(self, doc_ref: str) -> bytes:
        row = self.connection.execute(
            "SELECT data FROM documents JOIN blobs USING(content_hash) WHERE doc_ref = ?", (doc_ref,),
        ).fetchone()
        if row is None:
            raise KeyError(doc_ref)
        return bytes(row[0])

    def counts(self) -> dict[str, int]:
        tables = ("batches", "documents", "blobs", "extractions", "pages")
        return {table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
