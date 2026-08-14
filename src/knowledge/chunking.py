"""Semantic-ish chunking (Autonomous Upgrade Spec sec. 5.1: "Chunk by
semantic section rather than arbitrary fixed length when possible").

v1 splits on paragraph/heading boundaries and merges small paragraphs up to
a target size, rather than a blind fixed-character sliding window — cheap
to compute, no model dependency, and respects natural section breaks far
better than mid-sentence truncation would.
"""
from __future__ import annotations

import re

TARGET_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 200


def chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        candidate = f"{buffer}\n\n{para}" if buffer else para
        if len(candidate) >= TARGET_CHUNK_CHARS and len(buffer) >= MIN_CHUNK_CHARS:
            chunks.append(buffer)
            buffer = para
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)

    # A single paragraph longer than the target on its own still needs a
    # hard split so no chunk becomes unmanageably large for scoring/retrieval.
    final: list[str] = []
    for chunk in chunks:
        if len(chunk) <= TARGET_CHUNK_CHARS * 2:
            final.append(chunk)
        else:
            for i in range(0, len(chunk), TARGET_CHUNK_CHARS):
                final.append(chunk[i : i + TARGET_CHUNK_CHARS])
    return final
