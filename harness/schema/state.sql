CREATE TABLE batches (
    name TEXT PRIMARY KEY,
    first_ingested_at TEXT NOT NULL
);

CREATE TABLE blobs (
    content_hash TEXT PRIMARY KEY CHECK(length(content_hash) = 64),
    data BLOB NOT NULL
);

CREATE TABLE extractions (
    content_hash TEXT NOT NULL REFERENCES blobs(content_hash),
    format TEXT NOT NULL,
    reader_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready', 'failed', 'unsupported')),
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts >= 1),
    PRIMARY KEY (content_hash, format, reader_version),
    CHECK((status = 'ready' AND error IS NULL) OR (status != 'ready' AND error IS NOT NULL))
);

CREATE TABLE documents (
    doc_ref TEXT PRIMARY KEY,
    batch TEXT NOT NULL REFERENCES batches(name),
    kind TEXT NOT NULL CHECK(kind IN ('email', 'attachment')),
    content_hash TEXT NOT NULL REFERENCES blobs(content_hash),
    format TEXT NOT NULL,
    media_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    parent_ref TEXT REFERENCES documents(doc_ref),
    filename TEXT,
    attachment_index INTEGER,
    reader_version TEXT NOT NULL,
    UNIQUE (parent_ref, filename),
    FOREIGN KEY (content_hash, format, reader_version)
        REFERENCES extractions(content_hash, format, reader_version),
    CHECK((kind = 'email' AND parent_ref IS NULL AND filename IS NULL AND attachment_index IS NULL) OR
          (kind = 'attachment' AND parent_ref IS NOT NULL AND filename IS NOT NULL AND
           attachment_index IS NOT NULL AND attachment_index >= 1))
);

CREATE INDEX documents_received_at ON documents(received_at);
CREATE INDEX documents_content_hash ON documents(content_hash);
CREATE INDEX documents_batch ON documents(batch);

CREATE TABLE pages (
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    reader_version TEXT NOT NULL,
    number INTEGER NOT NULL CHECK(number >= 1),
    text TEXT NOT NULL,
    native_text TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('grader', 'ocr', 'html')),
    regions_json TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    PRIMARY KEY (content_hash, format, reader_version, number),
    FOREIGN KEY (content_hash, format, reader_version)
        REFERENCES extractions(content_hash, format, reader_version)
);

PRAGMA user_version = 1;
