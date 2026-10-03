"""Snapshots: create in Qdrant, download with a SHA-256 sidecar, rotate, copy offsite, restore.

A backup set is the Qdrant snapshot plus the manifest and the memory journal, so a
restore brings back vectors, bookkeeping and the runtime memories together.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import store
from .config import settings


def _headers() -> dict:
    key = settings().qdrant_api_key
    return {"api-key": key} if key else {}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def create(collection: str | None = None) -> dict:
    cfg = settings()
    name = collection or cfg.collection
    snap = store.client().create_snapshot(collection_name=name, wait=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = cfg.snapshot_dir / f"{name}-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    snap_file = target / snap.name
    with requests.get(f"{cfg.qdrant_url}/collections/{name}/snapshots/{snap.name}", headers=_headers(), stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with snap_file.open("wb") as fh:
            for block in resp.iter_content(1 << 20):
                fh.write(block)
    for extra in (cfg.manifest_db, cfg.memory_journal):
        if extra.exists():
            shutil.copy2(extra, target / extra.name)
    meta = {
        "collection": name,
        "snapshot": snap.name,
        "created_at": stamp,
        "bytes": snap_file.stat().st_size,
        "sha256": _sha256(snap_file),
        "points": store.count(name),
        "server_version": store.server_version(),
    }
    (target / "backup.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    store.client().delete_snapshot(collection_name=name, snapshot_name=snap.name, wait=True)
    offsite = []
    for dest in cfg.extra_offsite_backup:
        try:
            shutil.copytree(target, dest / target.name, dirs_exist_ok=True)
            offsite.append(str(dest / target.name))
        except OSError as exc:
            offsite.append(f"FAILED {dest}: {exc}")
    meta["offsite"] = offsite
    meta["path"] = str(target)
    rotate(name)
    return meta


def list_backups(collection: str | None = None) -> list[dict]:
    cfg = settings()
    name = collection or cfg.collection
    if not cfg.snapshot_dir.exists():
        return []
    rows = []
    for meta_file in sorted(cfg.snapshot_dir.glob(f"{name}-*/backup.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["path"] = str(meta_file.parent)
        rows.append(meta)
    return rows


def latest(collection: str | None = None) -> dict | None:
    rows = list_backups(collection)
    return rows[-1] if rows else None


def verify(meta: dict) -> bool:
    snap = Path(meta["path"]) / meta["snapshot"]
    return snap.exists() and snap.stat().st_size == meta["bytes"] and _sha256(snap) == meta["sha256"]


def rotate(collection: str | None = None) -> None:
    keep = settings().snapshot_keep
    rows = list_backups(collection)
    for meta in rows[:-keep] if len(rows) > keep else []:
        shutil.rmtree(meta["path"], ignore_errors=True)


def restore(meta: dict | None = None, collection: str | None = None, target_collection: str | None = None) -> dict:
    """Upload a snapshot back into Qdrant. target_collection lets the evaluator test a restore without touching live data."""
    cfg = settings()
    meta = meta or latest(collection)
    if not meta:
        raise RuntimeError("No backup found.")
    if not verify(meta):
        raise RuntimeError(f"Backup {meta['path']} failed its checksum. Refusing to restore.")
    name = target_collection or meta["collection"]
    snap = Path(meta["path"]) / meta["snapshot"]
    with snap.open("rb") as fh:
        resp = requests.post(
            f"{cfg.qdrant_url}/collections/{name}/snapshots/upload?priority=snapshot&wait=true",
            headers=_headers(),
            files={"snapshot": (snap.name, fh, "application/octet-stream")},
            timeout=1800,
        )
    resp.raise_for_status()
    if not target_collection:
        for extra in (cfg.manifest_db, cfg.memory_journal):
            src = Path(meta["path"]) / extra.name
            if src.exists():
                shutil.copy2(src, extra)
    return {"ok": True, "restored_into": name, "points": store.count(name), "from": meta["path"]}
