"""RAG retrieval over the knowledge library.

v1 uses TF-IDF (scikit-learn, already a project dependency) rather than
neural embeddings — a deliberate lean choice to avoid pulling in
sentence-transformers/torch (hundreds of MB) for what's currently a
personal-scale document corpus. It's refit on every query, which is fine at
dozens-to-low-hundreds of chunks; if the corpus grows large enough for that
to matter, that's the point to add a persisted vector index, not before.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import knowledge_chunks, knowledge_documents


@dataclass(frozen=True)
class RetrievedChunk:
    document_id: int
    chunk_id: int
    text: str
    similarity: float
    document_title: str | None
    source_domain: str | None
    overall_score: float | None


def search(engine: Engine, query: str, top_k: int = 8) -> list[RetrievedChunk]:
    with engine.connect() as conn:
        chunk_rows = conn.execute(select(knowledge_chunks)).mappings().all()
        doc_rows = {
            d["id"]: d for d in conn.execute(select(knowledge_documents)).mappings().all()
        }

    if not chunk_rows:
        return []

    texts = [c["text"] for c in chunk_rows]
    vectorizer = TfidfVectorizer(stop_words="english", max_features=20000)
    matrix = vectorizer.fit_transform(texts + [query])
    query_vec = matrix[-1]
    corpus_vecs = matrix[:-1]

    similarities = cosine_similarity(query_vec, corpus_vecs)[0]
    ranked_idx = np.argsort(-similarities)[:top_k]

    results = []
    for i in ranked_idx:
        if similarities[i] <= 0:
            continue
        chunk = chunk_rows[i]
        doc = doc_rows.get(chunk["document_id"], {})
        results.append(
            RetrievedChunk(
                document_id=chunk["document_id"],
                chunk_id=chunk["id"],
                text=chunk["text"],
                similarity=float(similarities[i]),
                document_title=doc.get("title"),
                source_domain=doc.get("source_domain"),
                overall_score=doc.get("overall_score"),
            )
        )
    return results
