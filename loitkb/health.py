"""Health checks. FAIL is loud: it is written to health.json, appended to the history,
returned to LIQA, and (optionally) posted to a Teams/Slack webhook.

The canary is the important one. It writes a known document, finds it through the
full hybrid + ACL path, reads its payload back, and deletes it. A store that lists
points but cannot read them (the 2026-10-03 corruption) fails here immediately.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone

from . import backup, manifest, querylog, store
from .config import settings

CANARY_GRANT = "loitkb:canary"


def _check(name: str, fn) -> dict:
    started = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:  # a crashed check is a failed check
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"[:400]
    return {"check": name, "status": status, "detail": detail, "ms": round((time.perf_counter() - started) * 1000, 1)}


def _server():
    version = store.server_version()
    return ("OK" if version else "FAIL"), {"version": version, "url": settings().qdrant_url}


def _collection():
    cfg = settings()
    if not store.client().collection_exists(cfg.collection):
        return "FAIL", "collection missing; run: python -m loitkb sync"
    info = store.client().get_collection(cfg.collection)
    vectors = info.config.params.vectors
    sparse = info.config.params.sparse_vectors or {}
    ok = store.DENSE in vectors and store.SPARSE in sparse and str(info.status).lower().endswith("green")
    return ("OK" if ok else "WARN"), {"status": str(info.status), "points": info.points_count, "indexed_vectors": info.indexed_vectors_count, "segments": info.segments_count}


def _consistency():
    cfg = settings()
    expected = manifest.total_chunks(cfg.collection)
    actual = store.count(cfg.collection)
    if expected == actual:
        return "OK", {"manifest_chunks": expected, "points": actual}
    return "FAIL", {"manifest_chunks": expected, "points": actual, "fix": "python -m loitkb sync --force or restore"}


def _canary():
    from .retrieve import retrieve
    from .sources import Document
    from .sync import chunk_records

    cfg = settings()
    token = f"canary{uuid.uuid4().hex[:10]}"
    doc = Document(
        doc_id=f"canary:{token}",
        source_type="canary",
        title="LOITKB canary",
        kind="text",
        text=f"The LOITKB health canary code is {token}. It proves vectors and payloads can be written and read back.",
        acl=[CANARY_GRANT],
        meta={"kind_label": "canary"},
    )
    try:
        store.upsert_chunks(doc.doc_id, chunk_records(doc), cfg.collection)
        result = retrieve(f"health canary code {token}", "canary", grants=[CANARY_GRANT], limit=1, log=False)
        top = result["hits"][0] if result["hits"] else {}
        ok = token in (top.get("text") or "")
    finally:
        store.delete_doc(doc.doc_id, cfg.collection)
    return ("OK" if ok else "FAIL"), {"found": ok, "latency_ms": result["latency_ms"]}


def _disk():
    cfg = settings()
    free = shutil.disk_usage(cfg.data_dir if cfg.data_dir.exists() else cfg.data_dir.anchor).free / 1e9
    return ("OK" if free >= cfg.min_free_disk_gb else "WARN"), {"free_gb": round(free, 1), "min_gb": cfg.min_free_disk_gb}


def _storage_mount():
    """Qdrant must run on a Docker-managed volume, not a Windows bind mount."""
    out = subprocess.run(
        ["docker", "inspect", "loitkb-qdrant", "--format", "{{range .Mounts}}{{.Type}}={{.Destination}};{{end}}"],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        return "WARN", "container loitkb-qdrant not found (not running under the bundled compose file)"
    mounts = out.stdout.strip()
    ok = "volume=/qdrant/storage" in mounts
    return ("OK" if ok else "FAIL"), {"mounts": mounts}


def _backup():
    cfg = settings()
    meta = backup.latest()
    if not meta:
        return "WARN", "no backup yet; run: python -m loitkb backup"
    created = datetime.strptime(meta["created_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    status = "OK" if age <= cfg.snapshot_max_age_hours else "WARN"
    return status, {"latest": meta["path"], "age_hours": round(age, 1), "points": meta["points"], "offsite": meta.get("offsite", [])}


def _queries():
    s = querylog.stats(24)
    if not s.get("queries"):
        return "OK", s
    status = "WARN" if (s.get("latency_p95_ms") or 0) > 3000 or (s.get("abstain_rate") or 0) > 0.5 else "OK"
    return status, s


CHECKS = [
    ("server", _server),
    ("collection", _collection),
    ("consistency", _consistency),
    ("canary_roundtrip", _canary),
    ("storage_mount", _storage_mount),
    ("disk", _disk),
    ("backup", _backup),
    ("query_log_24h", _queries),
]


def run(write: bool = True, alert: bool = True) -> dict:
    results = [_check(name, fn) for name, fn in CHECKS]
    statuses = {r["status"] for r in results}
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "OK"
    report = {"at": datetime.now(timezone.utc).isoformat(), "overall": overall, "checks": results}
    if write:
        cfg = settings()
        cfg.health_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.health_file.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        cfg.health_history.parent.mkdir(parents=True, exist_ok=True)
        with cfg.health_history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": report["at"], "overall": overall, "failed": [r["check"] for r in results if r["status"] != "OK"]}) + "\n")
    if alert and overall == "FAIL":
        notify(f"LOITKB health FAIL: {', '.join(r['check'] for r in results if r['status'] == 'FAIL')}")
    return report


def notify(message: str) -> None:
    hook = settings().alert_webhook
    if not hook:
        return
    try:
        import requests

        requests.post(hook, json={"text": message}, timeout=15)
    except Exception:
        pass
