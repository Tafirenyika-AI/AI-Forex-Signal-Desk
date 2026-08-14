"""Document ingestion pipeline (Autonomous Upgrade Spec sec. 5.1).

hash -> extract -> score -> novelty-check -> chunk -> classify -> extract
candidate hypotheses -> store. Idempotent on content hash: re-running over
an unchanged file is a no-op, a changed file re-ingests under the same
filepath (old chunks replaced), consistent with the "CREATE/MODIFY" watcher
behavior the spec asks for even though this runs as a periodic scan
(src/scripts/ingest_knowledge.py) rather than a live filesystem watcher —
see project_autonomous_upgrade_roadmap memory for why.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from src.data.db import knowledge_chunks, knowledge_documents
from src.data.db import upsert_insert as insert
from src.knowledge.chunking import chunk_text
from src.knowledge.extract import extract_text, guess_title
from src.knowledge.hypotheses import classify_topic, extract_candidate_hypotheses
from src.knowledge.retrieval import search
from src.knowledge.scoring import score_document

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".markdown"}


@dataclass
class IngestionResult:
    filepath: str
    status: str  # "ingested" / "unchanged" / "skipped" / "error"
    reason: str = ""
    chunk_count: int = 0
    overall_score: float | None = None
    novelty_score: float | None = None
    new_hypotheses: list[str] | None = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _category_from_path(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
        return rel.parts[0] if rel.parts else "uncategorized"
    except ValueError:
        return "uncategorized"


def ingest_document(engine: Engine, path: Path, knowledge_root: Path) -> IngestionResult:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return IngestionResult(str(path), "skipped", reason=f"unsupported type {path.suffix}")

    file_hash = _sha256(path)
    with engine.connect() as conn:
        existing = conn.execute(
            select(knowledge_documents).where(knowledge_documents.c.sha256 == file_hash)
        ).mappings().first()
    if existing:
        return IngestionResult(str(path), "unchanged", chunk_count=existing["chunk_count"],
                                overall_score=existing["overall_score"])

    try:
        text, doc_type = extract_text(path)
    except Exception as exc:  # noqa: BLE001
        return IngestionResult(str(path), "error", reason=repr(exc))

    if not text.strip():
        return IngestionResult(str(path), "error", reason="no extractable text")

    title = guess_title(text, fallback=path.stem)
    category = _category_from_path(path, knowledge_root)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    scores = score_document(text, source_domain=None, publication_date=mtime)

    # Novelty: how different is this from what's already indexed? Compare
    # the new document's own text as a query against the EXISTING corpus
    # (before this document's chunks are added) — high similarity to
    # something already indexed means low novelty.
    existing_matches = search(engine, text[:2000], top_k=1)
    novelty = 1.0 - existing_matches[0].similarity if existing_matches else 1.0

    chunks = chunk_text(text)
    hypotheses = extract_candidate_hypotheses(text)
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        # Replace-on-change: if this filepath was previously ingested under a
        # different hash (content changed), clear its old chunks first.
        old = conn.execute(
            select(knowledge_documents).where(knowledge_documents.c.filepath == str(path))
        ).mappings().first()
        if old:
            conn.execute(delete(knowledge_chunks).where(knowledge_chunks.c.document_id == old["id"]))
            conn.execute(delete(knowledge_documents).where(knowledge_documents.c.id == old["id"]))

        doc_result = conn.execute(
            insert(knowledge_documents).values(
                filepath=str(path),
                sha256=file_hash,
                title=title,
                source_domain=None,
                document_type=doc_type,
                category=category,
                publication_date=mtime,
                ingested_at=now,
                source_score_json=json.dumps(scores),
                overall_score=scores["overall"],
                novelty_score=novelty,
                chunk_count=len(chunks),
                candidate_hypotheses_json=json.dumps(hypotheses),
            )
        )
        document_id = doc_result.inserted_primary_key[0]

        for i, chunk in enumerate(chunks):
            conn.execute(
                insert(knowledge_chunks).values(
                    document_id=document_id, chunk_index=i, text=chunk, topic=classify_topic(chunk),
                )
            )

    return IngestionResult(
        str(path), "ingested", chunk_count=len(chunks),
        overall_score=scores["overall"], novelty_score=novelty, new_hypotheses=hypotheses,
    )


def scan_and_ingest(engine: Engine, knowledge_root: Path) -> list[IngestionResult]:
    results = []
    for path in sorted(knowledge_root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            results.append(ingest_document(engine, path, knowledge_root))
    return results
