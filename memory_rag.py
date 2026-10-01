"""Durable LIQA memory. Qdrant on disk. Local embeddings. No secrets."""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from paths import REPO_ROOT
except ImportError:
    REPO_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "mcp" else Path(__file__).resolve().parent

QDRANT_URL = os.environ.get("LIQA_QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = "liqa_memory"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
VECTOR_SIZE = 384
CHUNK = 480
OVERLAP = 60

DATA = Path(os.environ.get("LIQA_MEMORY_DIR", r"C:\LIQA-memory"))
MODEL_CACHE = DATA / "models"
MANIFEST = DATA / "memory-manifest.json"

# Live hooks. Locks (Sigiri, Book1) stay in code. RAG only recalls and remembers.
WIRED = [
    {"place": "liqa_boot", "phase": "planning", "mode": "read", "why": "Initial step. Recall laws and lessons before a Jira key is opened."},
    {"place": "liqa_learn_speed", "phase": "planning", "mode": "read", "why": "Planning shortcuts from every prior round."},
    {"place": "liqa_learn_record", "phase": "completion", "mode": "write", "why": "Write what worked so the next run cannot forget it."},
    {"place": "liqa_learn_cycle", "phase": "completion", "mode": "write", "why": "Closeout writes the round into permanent memory."},
    {"place": "liqa_memory_recall", "phase": "any", "mode": "read", "why": "Any phase can ask the memory by hand."},
    {"place": "liqa_memory_remember", "phase": "any", "mode": "write", "why": "Any phase can store a fact that must survive for years."},
]

def _roots() -> list[Path]:
    homes = [REPO_ROOT]
    extra = Path(os.environ.get("LIQA_HOME", r"E:\LIQA"))
    if extra.exists() and extra.resolve() != REPO_ROOT.resolve():
        homes.append(extra)
    roots: list[Path] = []
    for home in homes:
        roots.extend(
            [
                home / "docs",
                home / "mcp",
                home / "skills",
                home / "packaging",
                home / "company",
                home / "apps",
            ]
        )
    roots.append(Path(os.environ.get("LIQA_AGENCY_HOME", r"E:\agency-agents")) / ".cursor" / "rules")
    return roots

SKIP = re.compile(r"(__pycache__|node_modules|secrets|\.env|creds|password|captures)", re.I)
_embedder = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=QDRANT_URL, timeout=120, check_compatibility=False)


def _model():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        _embedder = TextEmbedding(MODEL_NAME, cache_dir=str(MODEL_CACHE))
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    return [vec.tolist() for vec in _model().embed(texts)]


def _point_id(key: str) -> str:
    return str(uuid.UUID(bytes=hashlib.sha256(key.encode("utf-8")).digest()[:16]))


def ensure_collection(client=None) -> None:
    from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

    client = client or _client()
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
    for field in ("phase", "kind", "place"):
        try:
            client.create_payload_index(COLLECTION, field, field_schema=PayloadSchemaType.KEYWORD)
        except Exception:
            pass


def _windows(text: str) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 80:
        return []
    if len(text) <= CHUNK:
        return [text]
    out: list[str] = []
    step = CHUNK - OVERLAP
    i = 0
    while i < len(text) and len(out) < 400:
        piece = text[i : i + CHUNK].strip()
        if len(piece) >= 80:
            out.append(piece)
        i += step
    return out


def iter_files() -> list[Path]:
    found: list[Path] = []
    for root in _roots():
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".mdc", ".py", ".yaml", ".yml"}:
                continue
            if SKIP.search(str(path)):
                continue
            if path.stat().st_size > 1_500_000:
                continue
            found.append(path)
    return found


def architecture_records() -> list[dict[str, Any]]:
    """One record per flow step and per live RAG hook. Studied from laws.ISTQB_PHASES."""
    try:
        from laws import ISTQB_PHASES
    except ImportError:
        ISTQB_PHASES = []

    rows: list[dict[str, Any]] = []
    for phase in ISTQB_PHASES:
        for n, step in enumerate(phase["do"], start=1):
            rows.append(
                {
                    "key": f"phase:{phase['key']}:{n}",
                    "text": f"LIQA phase {phase['id']} {phase['title']} ({phase['key']}). Step {n}: {step} ISTQB: {phase['istqb']}",
                    "payload": {
                        "kind": "flow_step",
                        "phase": phase["key"],
                        "place": f"phase-{phase['id']}-step-{n}",
                        "source": "mcp/laws.py",
                    },
                }
            )
    for hook in WIRED:
        rows.append(
            {
                "key": f"wire:{hook['place']}",
                "text": f"RAG hook {hook['place']} phase {hook['phase']} mode {hook['mode']}. {hook['why']}",
                "payload": {
                    "kind": "rag_hook",
                    "phase": hook["phase"],
                    "place": hook["place"],
                    "source": "mcp/memory_rag.py",
                },
            }
        )
    return rows


def corpus_records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(path)
        for n, piece in enumerate(_windows(text)):
            rows.append(
                {
                    "key": f"file:{rel}:{n}",
                    "text": piece,
                    "payload": {
                        "kind": "source",
                        "phase": "knowledge",
                        "place": rel,
                        "source": rel,
                        "chunk": n,
                    },
                }
            )
    return rows


def upsert(records: list[dict[str, Any]], client=None) -> int:
    from qdrant_client.models import PointStruct

    client = client or _client()
    ensure_collection(client)
    done = 0
    batch = 64
    for i in range(0, len(records), batch):
        chunk = records[i : i + batch]
        vectors = _embed([r["text"] for r in chunk])
        points = []
        for rec, vec in zip(chunk, vectors):
            payload = dict(rec["payload"])
            payload["text"] = rec["text"]
            payload["ingested_at"] = _now()
            points.append(PointStruct(id=_point_id(rec["key"]), vector=vec, payload=payload))
        client.upsert(collection_name=COLLECTION, points=points)
        done += len(points)
    return done


def recall(query: str, phase: str = "", limit: int = 6) -> dict[str, Any]:
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = _client()
    qfilter = None
    if phase:
        qfilter = Filter(must=[FieldCondition(key="phase", match=MatchValue(value=phase))])
    hits = client.query_points(
        collection_name=COLLECTION,
        query=_embed([query])[0],
        query_filter=qfilter,
        limit=max(1, min(int(limit), 12)),
    ).points
    return {
        "ok": True,
        "query": query,
        "hits": [
            {
                "score": round(float(h.score or 0), 4),
                "phase": (h.payload or {}).get("phase"),
                "kind": (h.payload or {}).get("kind"),
                "place": (h.payload or {}).get("place"),
                "text": ((h.payload or {}).get("text") or "")[:700],
            }
            for h in hits
        ],
    }


def remember(text: str, kind: str = "memory", phase: str = "completion", task_key: str = "", place: str = "liqa_memory_remember") -> dict[str, Any]:
    body = (text or "").strip()
    if len(body) < 8:
        return {"ok": False, "error": "text too short"}
    key = f"mem:{task_key}:{_now()}:{hashlib.sha256(body.encode()).hexdigest()[:12]}"
    n = upsert(
        [
            {
                "key": key,
                "text": body,
                "payload": {"kind": kind, "phase": phase or "completion", "place": place, "source": task_key or "runtime", "task_key": task_key},
            }
        ]
    )
    return {"ok": True, "stored": n, "key": key}


def status() -> dict[str, Any]:
    client = _client()
    info = client.get_collection(COLLECTION)
    return {
        "ok": True,
        "url": QDRANT_URL,
        "collection": COLLECTION,
        "points": info.points_count,
        "disk": str(DATA / "qdrant"),
        "model": MODEL_NAME,
        "wired": WIRED,
        "durability": "Vectors live on disk under C:\\LIQA-memory\\qdrant. Container restarts with Docker. Copy that folder to keep the memory if the disk is replaced.",
    }
