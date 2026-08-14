"""Text extraction for the knowledge library (Autonomous Upgrade Spec sec. 5.1
"Extract title, author, publisher/domain, publication date... document type").

Supports the formats a personal research folder actually accumulates: PDF,
DOCX (parsed via raw XML — no python-docx dependency needed, the same
approach used to read the spec document itself), TXT, and MD. Anything else
is skipped with an honest reason rather than guessed at.
"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from pypdf import PdfReader

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_text(path: Path) -> tuple[str, str]:
    """Returns (text, document_type). Raises ValueError for unsupported types."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path), "pdf"
    if suffix == ".docx":
        return _extract_docx(path), "docx"
    if suffix in (".txt",):
        return path.read_text(encoding="utf-8", errors="replace"), "txt"
    if suffix in (".md", ".markdown"):
        return path.read_text(encoding="utf-8", errors="replace"), "md"
    raise ValueError(f"Unsupported document type: {suffix}")


def _extract_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml_content = z.read("word/document.xml")
    root = ET.fromstring(xml_content)
    paragraphs = []
    for p in root.iter(f"{WORD_NS}p"):
        texts = [t.text or "" for t in p.iter(f"{WORD_NS}t")]
        paragraphs.append("".join(texts))
    return "\n".join(paragraphs)


def guess_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) >= 8:
            return stripped[:200]
    return fallback
