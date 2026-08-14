"""Scans forex_knowledge/ for new or changed files and ingests them into the
RAG knowledge library (Autonomous Upgrade Spec sec. 5).

Run from the project root with the venv active:
    python -m src.scripts.ingest_knowledge
"""
from __future__ import annotations

from pathlib import Path

from src.config import load_settings
from src.data.db import get_engine
from src.knowledge.ingest import scan_and_ingest

KNOWLEDGE_ROOT = Path(__file__).resolve().parent.parent.parent / "forex_knowledge"


def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    if not KNOWLEDGE_ROOT.exists():
        print(f"{KNOWLEDGE_ROOT} does not exist — nothing to ingest.")
        return

    results = scan_and_ingest(engine, KNOWLEDGE_ROOT)
    ingested = [r for r in results if r.status == "ingested"]
    unchanged = [r for r in results if r.status == "unchanged"]
    errors = [r for r in results if r.status == "error"]
    skipped = [r for r in results if r.status == "skipped"]

    print(f"Scanned {len(results)} files: {len(ingested)} newly ingested, "
          f"{len(unchanged)} unchanged, {len(errors)} errors, {len(skipped)} skipped (unsupported type).")

    for r in ingested:
        print(f"\n  NEW: {r.filepath}")
        print(f"    chunks={r.chunk_count} overall_score={r.overall_score:.2f} novelty={r.novelty_score:.2f}")
        if r.new_hypotheses:
            print(f"    candidate hypotheses ({len(r.new_hypotheses)}):")
            for h in r.new_hypotheses[:5]:
                print(f"      - {h}")

    if errors:
        print("\nErrors:")
        for r in errors:
            print(f"  {r.filepath}: {r.reason}")


if __name__ == "__main__":
    main()
