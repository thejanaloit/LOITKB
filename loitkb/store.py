"""Qdrant schema, writes, hybrid search and snapshots."""
from __future__ import annotations

import hashlib
import uuid
import warnings
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from .config import settings

DENSE = "dense"
SPARSE = "bm25"
INDEXED_FIELDS = ("doc_id", "source_type", "kind", "phase", "acl", "project", "issue_type", "place")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def point_id(doc_id: str, index: int) -> str:
    return str(uuid.UUID(bytes=hashlib.sha256(f"{doc_id}#{index}".encode("utf-8")).digest()[:16]))


@lru_cache(maxsize=1)
def client():
    from qdrant_client import QdrantClient

    cfg = settings()
    if cfg.qdrant_url.startswith(("http://127.0.0.1", "http://localhost")):
        # Qdrant binds to loopback only; plain HTTP never leaves the machine.
        warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
    return QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None, timeout=120)


def ensure_collection(name: str | None = None) -> None:
    from qdrant_client.models import (
        Distance,
        HnswConfigDiff,
        Modifier,
        OptimizersConfigDiff,
        PayloadSchemaType,
        SparseVectorParams,
        VectorParams,
    )

    cfg = settings()
    name = name or cfg.collection
    c = client()
    if not c.collection_exists(name):
        c.create_collection(
            collection_name=name,
            vectors_config={DENSE: VectorParams(size=cfg.dense_size, distance=Distance.COSINE)},
            sparse_vectors_config={SPARSE: SparseVectorParams(modifier=Modifier.IDF)},
            hnsw_config=HnswConfigDiff(m=16, ef_construct=128),
            optimizers_config=OptimizersConfigDiff(indexing_threshold=1000),
            on_disk_payload=True,
        )
        for field_name in INDEXED_FIELDS:
            c.create_payload_index(name, field_name, field_schema=PayloadSchemaType.KEYWORD)


def upsert_chunks(doc_id: str, records: list[dict[str, Any]], collection: str | None = None) -> int:
    """records: [{"text", "embed_text", "payload"}]. Point ids derive from doc_id + index."""
    from qdrant_client.models import PointStruct

    from .embed import embed_dense, embed_sparse

    if not records:
        return 0
    name = collection or settings().collection
    total = 0
    for start in range(0, len(records), 64):
        batch = records[start : start + 64]
        texts = [r.get("embed_text") or r["text"] for r in batch]
        dense = embed_dense(texts)
        sparse = embed_sparse(texts)
        points = []
        for offset, (rec, d, s) in enumerate(zip(batch, dense, sparse)):
            payload = dict(rec["payload"])
            payload.update({"text": rec["text"], "doc_id": doc_id, "chunk_index": start + offset, "ingested_at": now()})
            points.append(PointStruct(id=point_id(doc_id, start + offset), vector={DENSE: d, SPARSE: s}, payload=payload))
        client().upsert(collection_name=name, points=points, wait=True)
        total += len(points)
    return total


def delete_doc(doc_id: str, collection: str | None = None) -> None:
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

    client().delete(
        collection_name=collection or settings().collection,
        points_selector=FilterSelector(filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))])),
        wait=True,
    )


def count(collection: str | None = None, must: Iterable | None = None) -> int:
    from qdrant_client.models import Filter

    flt = Filter(must=list(must)) if must else None
    return client().count(collection_name=collection or settings().collection, count_filter=flt, exact=True).count


def search(
    query: str,
    grants: list[str],
    *,
    mode: str = "hybrid",
    limit: int = 30,
    must: list | None = None,
    collection: str | None = None,
) -> list[dict[str, Any]]:
    """mode: hybrid (dense + BM25 fused with RRF), dense, or sparse. ACL filter always applied."""
    from qdrant_client.models import Filter, Fusion, FusionQuery, Prefetch

    from .acl import qdrant_filter
    from .embed import embed_dense_query, embed_sparse_query

    if not grants:
        return []
    flt = Filter(must=[qdrant_filter(grants), *(must or [])])
    name = collection or settings().collection
    c = client()
    if mode == "dense":
        res = c.query_points(name, query=embed_dense_query(query), using=DENSE, query_filter=flt, limit=limit, with_payload=True)
    elif mode == "sparse":
        res = c.query_points(name, query=embed_sparse_query(query), using=SPARSE, query_filter=flt, limit=limit, with_payload=True)
    else:
        res = c.query_points(
            name,
            prefetch=[
                Prefetch(query=embed_dense_query(query), using=DENSE, filter=flt, limit=limit * 2),
                Prefetch(query=embed_sparse_query(query), using=SPARSE, filter=flt, limit=limit * 2),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=flt,
            limit=limit,
            with_payload=True,
        )
    return [{"id": str(p.id), "score": float(p.score or 0.0), "payload": p.payload or {}} for p in res.points]


def server_version() -> str:
    import requests

    cfg = settings()
    headers = {"api-key": cfg.qdrant_api_key} if cfg.qdrant_api_key else {}
    return requests.get(cfg.qdrant_url, headers=headers, timeout=10).json().get("version", "")
