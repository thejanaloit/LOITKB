"""Incremental sync. Unchanged documents are skipped, changed ones are replaced
(delete by doc_id, then insert), and documents that vanished from a source are
deleted from the index. Re-running is idempotent.

Every write goes through the guards: one writer at a time, enough free disk, and
the index schema/model signature must match the config. A source that suddenly
returns far fewer documents (wrong LIQA_HOME, unmounted drive) deletes nothing.
"""
from __future__ import annotations

import time
from typing import Iterable

from . import acl as acl_mod
from . import guard, manifest, store
from .chunking import chunk_document
from .config import settings
from .sources import SOURCES, Document, redact


def chunk_records(doc: Document) -> list[dict]:
    cfg = settings()
    chunks = chunk_document(redact(doc.text), doc.title, doc.kind, cfg.chunk_max_chars, cfg.chunk_overlap_chars, cfg.chunk_min_chars)
    tags = acl_mod.expand_tags(doc.acl)
    records = []
    for ch in chunks:
        payload = {
            "source_type": doc.source_type,
            "title": doc.title,
            "heading": ch.heading,
            "acl": tags,
            "content_hash": doc.content_hash,
            "kind": doc.meta.get("kind_label", doc.source_type),
            "phase": doc.meta.get("phase", "knowledge"),
            "place": doc.meta.get("place", doc.doc_id),
            "source": doc.meta.get("source", doc.doc_id),
        }
        payload.update({k: v for k, v in doc.meta.items() if k not in payload and k != "kind_label"})
        records.append({"text": ch.text, "embed_text": ch.embed_text, "payload": payload})
    return records


def _index(doc: Document, name: str, force: bool, owner: str) -> str:
    known = manifest.get_all(name).get(doc.doc_id)
    if known and known[0] == doc.content_hash and not force:
        if manifest.get_all(name, owner).get(doc.doc_id) is None:
            manifest.put(name, doc.doc_id, owner, doc.content_hash, known[1], store.now())
        return "skipped"
    records = chunk_records(doc)
    store.delete_doc(doc.doc_id, name)
    n = store.upsert_chunks(doc.doc_id, records, name)
    manifest.put(name, doc.doc_id, owner, doc.content_hash, n, store.now())
    return "indexed" if n else "empty"


def index_document(doc: Document, collection: str | None = None, force: bool = False, source_type: str | None = None, lock_wait_s: float = 600) -> str:
    """Returns 'skipped' | 'indexed' | 'empty'.

    The manifest owner is the sync source label, so stale-document deletion for
    that source always finds what it indexed."""
    name = collection or settings().collection
    guard.require_disk("index a document")
    with guard.single_writer("index a document", name, wait_s=lock_wait_s):
        store.ensure_collection(name)
        guard.check_schema(name)
        return _index(doc, name, force, source_type or doc.source_type)


def sync_documents(docs: Iterable[Document], source_type: str, collection: str | None = None, delete_missing: bool = True, force: bool = False, log=print) -> dict:
    cfg = settings()
    name = collection or cfg.collection
    guard.require_disk(f"sync {source_type}")
    with guard.single_writer(f"sync {source_type}", name):
        store.ensure_collection(name)
        guard.check_schema(name)
        started = time.time()
        stats = {"source": source_type, "indexed": 0, "skipped": 0, "empty": 0, "deleted": 0}
        seen: set[str] = set()
        for doc in docs:
            seen.add(doc.doc_id)
            stats[_index(doc, name, force, source_type)] += 1
            done = stats["indexed"] + stats["skipped"] + stats["empty"]
            if done % 50 == 0:
                log(f"[{source_type}] {done} docs ({stats['indexed']} indexed, {stats['skipped']} unchanged)")
        if delete_missing:
            known = set(manifest.get_all(name, source_type))
            stale = known - seen
            limit = max(cfg.max_delete_floor, int(len(known) * cfg.max_delete_fraction))
            if len(stale) > limit:
                stats["deletion_blocked"] = (
                    f"{len(stale)} of {len(known)} '{source_type}' documents vanished (limit {limit}). "
                    "Treating the source as broken (check LIQA_HOME / drive). Nothing deleted. "
                    "If the removal is real, run: python -m loitkb rebuild"
                )
                log(stats["deletion_blocked"])
                try:
                    from .health import send_alert

                    send_alert("WARN", f"LOITKB sync deletion blocked: {stats['deletion_blocked']}")
                except Exception:
                    pass
            else:
                for doc_id in stale:
                    store.delete_doc(doc_id, name)
                    manifest.remove(name, doc_id)
                    stats["deleted"] += 1
        stats["seconds"] = round(time.time() - started, 1)
        return stats


def sync(sources: list[str] | None = None, collection: str | None = None, force: bool = False, log=print) -> list[dict]:
    results = []
    for source in sources or list(SOURCES):
        results.append(sync_documents(SOURCES[source](), source, collection, True, force, log))
        log(str(results[-1]))
    manifest.set_state("last_sync", store.now())
    return results


def rebuild(collection: str | None = None, log=print) -> list[dict]:
    """Drop the collection and re-index every source. Use after corruption or a schema change.

    A backup is taken first (when the collection is readable), so a bad rebuild can be undone
    with `python -m loitkb restore --yes`."""
    name = collection or settings().collection
    guard.require_disk("rebuild")
    pre = None
    if store.client().collection_exists(name):
        try:
            from .backup import create

            pre = create(name, tag="pre-rebuild")["path"]
            log(f"pre-rebuild backup: {pre}")
        except Exception as exc:
            log(f"pre-rebuild backup failed ({exc}); continuing because rebuild is the recovery path")
        with guard.single_writer("rebuild", name):
            store.client().delete_collection(name)
    manifest.clear(name)
    manifest.set_state(f"schema:{name}", "")
    return sync(collection=name, force=True, log=log)
