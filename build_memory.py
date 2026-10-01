"""Index LIQA flow + source into the on-disk Qdrant memory."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_rag import MANIFEST, architecture_records, corpus_records, status, upsert  # noqa: E402


def main() -> None:
    arch = architecture_records()
    corpus = corpus_records()
    records = arch + corpus
    print(f"pieces={len(records)} flow_steps={len(arch)} source_chunks={len(corpus)}", flush=True)
    stored = upsert(records)
    info = status()
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps({"stored": stored, "pieces": len(records), "status": info}, indent=2),
        encoding="utf-8",
    )
    print(f"stored={stored} points={info.get('points')}", flush=True)


if __name__ == "__main__":
    main()
