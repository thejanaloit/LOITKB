"""Every retrieval is logged locally (latency, scores, sources) for monitoring and drift."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from .config import settings


def record(result: dict) -> None:
    path = settings().query_log
    path.parent.mkdir(parents=True, exist_ok=True)
    hits = result.get("hits") or []
    row = {
        "at": datetime.now(timezone.utc).isoformat(),
        "principal": result.get("principal"),
        "query": result.get("query"),
        "mode": result.get("mode"),
        "reranked": result.get("reranked"),
        "degraded": result.get("degraded", ""),
        "abstain": result.get("abstain"),
        "latency_ms": (result.get("latency_ms") or {}).get("total"),
        "top_score": hits[0]["score"] if hits else None,
        "top_sources": [h.get("doc_id") for h in hits[:3]],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def stats(hours: float = 24) -> dict:
    path = settings().query_log
    if not path.exists():
        return {"queries": 0}
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-20000:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if datetime.fromisoformat(row["at"]) >= since:
            rows.append(row)
    if not rows:
        return {"queries": 0}
    lat = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)
    tops = [r["top_score"] for r in rows if r.get("top_score") is not None and r.get("reranked")]
    return {
        "queries": len(rows),
        "abstain_rate": round(sum(1 for r in rows if r.get("abstain")) / len(rows), 3),
        "degraded_rate": round(sum(1 for r in rows if r.get("degraded")) / len(rows), 3),
        "latency_p50_ms": lat[len(lat) // 2] if lat else None,
        "latency_p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
        "mean_top_rerank_score": round(statistics.fmean(tops), 3) if tops else None,
    }
