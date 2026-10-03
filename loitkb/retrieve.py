"""Query pipeline: ACL-filtered hybrid search -> cross-encoder rerank -> dedupe -> abstain."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from . import acl as acl_mod
from . import querylog, store
from .config import settings


def _dedupe(hits: list[dict], per_doc: int) -> list[dict]:
    out, seen_text, per = [], set(), {}
    for h in hits:
        p = h["payload"]
        key = (p.get("text") or "")[:200]
        if key in seen_text:
            continue
        doc = p.get("doc_id", h["id"])
        if per.get(doc, 0) >= per_doc:
            continue
        seen_text.add(key)
        per[doc] = per.get(doc, 0) + 1
        out.append(h)
    return out


_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")
_BUSY = threading.Lock()


def _rerank_within_budget(query: str, docs: list[str], budget_ms: int) -> list[float] | None:
    """Cross-encode in a worker; None if it is still busy or misses the budget."""
    from .embed import rerank as cross_encode
    from .embed import rerank_model

    rerank_model()  # one-time load is not a latency regression; keep it outside the budget
    if budget_ms <= 0:
        return cross_encode(query, docs)
    if not _BUSY.acquire(blocking=False):
        return None

    def job():
        try:
            return cross_encode(query, docs)
        finally:
            _BUSY.release()

    future = _POOL.submit(job)
    try:
        return future.result(timeout=budget_ms / 1000)
    except FutureTimeout:
        return None


def _rerank_text(payload: dict) -> str:
    """The cross-encoder needs the document title and heading to judge a chunk."""
    head = " > ".join(x for x in (payload.get("title"), payload.get("heading")) if x)
    return f"{head}\n{payload.get('text', '')}" if head else payload.get("text", "")


def retrieve(
    query: str,
    principal: str,
    *,
    limit: int = 6,
    mode: str = "hybrid",
    rerank: bool | None = None,
    must: list | None = None,
    collection: str | None = None,
    log: bool = True,
    grants: list[str] | None = None,
) -> dict[str, Any]:
    cfg = settings()
    rerank = cfg.rerank_enabled if rerank is None else rerank
    started = time.perf_counter()
    grants = grants if grants is not None else acl_mod.grants_for(principal)
    hits = store.search(query, grants, mode=mode, limit=cfg.candidates, must=must, collection=collection)
    t_search = time.perf_counter()
    degraded = ""
    best_rerank = None
    if rerank and hits:
        head, tail = hits[: max(1, cfg.rerank_top)], hits[max(1, cfg.rerank_top):]
        scores = _rerank_within_budget(query, [_rerank_text(h["payload"]) for h in head], cfg.rerank_budget_ms)
        if scores is None:
            degraded = "rerank_budget_exceeded"
            rerank = False
    if rerank and hits:
        by_rerank = sorted(range(len(head)), key=lambda i: scores[i], reverse=True)
        rerank_rank = {i: r for r, i in enumerate(by_rerank)}
        w = cfg.rerank_weight
        for i, (h, s) in enumerate(zip(head, scores)):
            h["fused_score"] = h["score"]
            h["rerank_score"] = s
            h["blend"] = w / (60 + rerank_rank[i]) + (1 - w) / (60 + i)
        best_rerank = scores[by_rerank[0]]
        head.sort(key=lambda h: h["blend"], reverse=True)
        for h in tail:
            h["fused_score"] = h["score"]
        hits = head + tail
    hits = _dedupe(hits, per_doc=2)[: max(1, min(limit, 20))]
    t_done = time.perf_counter()

    top = hits[0] if hits else None
    if not top:
        abstain = True
    elif rerank:
        abstain = best_rerank < cfg.abstain_rerank_score
    elif degraded:
        # Without the cross-encoder there is no calibrated confidence; return the
        # hybrid hits and let the caller see the degraded flag.
        abstain = False
    else:
        abstain = top["score"] <= cfg.abstain_fused_score

    result = {
        "ok": True,
        "query": query,
        "principal": principal,
        "mode": mode,
        "reranked": bool(rerank),
        "degraded": degraded,
        "abstain": abstain,
        "latency_ms": {"search": round((t_search - started) * 1000, 1), "total": round((t_done - started) * 1000, 1)},
        "hits": [
            {
                "rank": i + 1,
                "score": round(h.get("rerank_score", h["score"]), 4),
                "fused_score": round(h.get("fused_score", h["score"]), 4),
                "doc_id": h["payload"].get("doc_id"),
                "source": h["payload"].get("source"),
                "title": h["payload"].get("title"),
                "heading": h["payload"].get("heading"),
                "kind": h["payload"].get("kind"),
                "phase": h["payload"].get("phase"),
                "place": h["payload"].get("place"),
                "chunk_index": h["payload"].get("chunk_index"),
                "text": h["payload"].get("text", ""),
            }
            for i, h in enumerate(hits)
        ],
    }
    if log:
        querylog.record(result)
    return result
