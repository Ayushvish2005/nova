-- Additive migration: preserve every ingestion record and its original bytes.
CREATE TABLE fact_extractions (
    extraction_id TEXT PRIMARY KEY,
    doc_ref TEXT NOT NULL REFERENCES documents(doc_ref),
    reader_version TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts >= 1),
    error TEXT,
    issues_json TEXT NOT NULL,
    responses_json TEXT NOT NULL,
    UNIQUE(doc_ref, reader_version, extractor_version, input_hash),
    CHECK((status = 'ready' AND error IS NULL) OR (status = 'failed' AND error IS NOT NULL))
);

CREATE TABLE active_fact_extractions (
    doc_ref TEXT PRIMARY KEY REFERENCES documents(doc_ref),
    extraction_id TEXT NOT NULL REFERENCES fact_extractions(extraction_id)
);

CREATE TABLE facts (
    fact_id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL REFERENCES fact_extractions(extraction_id),
    doc_ref TEXT NOT NULL REFERENCES documents(doc_ref),
    kind TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX facts_source ON facts(doc_ref);
CREATE INDEX facts_entity ON facts(entity_type, entity_id);
CREATE INDEX facts_extraction ON facts(extraction_id);

CREATE TABLE fact_relationships (
    relationship_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_doc_ref TEXT REFERENCES documents(doc_ref)
);
CREATE INDEX fact_relationships_target ON fact_relationships(target_type, target_id);

PRAGMA user_version = 2;
