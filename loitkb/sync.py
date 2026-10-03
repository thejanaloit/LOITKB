"""Incremental sync. Unchanged documents are skipped, changed ones are replaced
(delete by doc_id, then insert), and documents that vanished from a source are
deleted from the index. Re-running is idempotent."""
from __future__ import annotations

import time
from typing import Iterable

from . import acl as acl_mod
from . import manifest, store
from .chunking import chunk_document
from .config import settings
from .sources import SOURCES, Document


def chunk_records(doc: Document) -> list[dict]:
    cfg = settings()
    chunks = chunk_document(doc.text, doc.title, doc.kind, cfg.chunk_max_chars, cfg.chunk_overlap_chars, cfg.chunk_min_chars)
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


def index_document(doc: Document, collection: str | None = None, force: bool = False, source_type: str | None = None) -> str:
    """Returns 'skipped' | 'indexed' | 'empty'.

    The manifest owner is the sync source label, so stale-document deletion for
    that source always finds what it indexed."""
    name = collection or settings().collection
    store.ensure_collection(name)
    owner = source_type or doc.source_type
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


def sync_documents(docs: Iterable[Document], source_type: str, collection: str | None = None, delete_missing: bool = True, force: bool = False, log=print) -> dict:
    name = collection or settings().collection
    store.ensure_collection(name)
    started = time.time()
    stats = {"source": source_type, "indexed": 0, "skipped": 0, "empty": 0, "deleted": 0}
    seen: set[str] = set()
    for doc in docs:
        seen.add(doc.doc_id)
        stats[index_document(doc, name, force, source_type)] += 1
        done = stats["indexed"] + stats["skipped"] + stats["empty"]
        if done % 50 == 0:
            log(f"[{source_type}] {done} docs ({stats['indexed']} indexed, {stats['skipped']} unchanged)")
    if delete_missing:
        for doc_id in set(manifest.get_all(name, source_type)) - seen:
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
    """Drop the collection and re-index every source. Use after corruption or a schema change."""
    name = collection or settings().collection
    if store.client().collection_exists(name):
        store.client().delete_collection(name)
    manifest.clear(name)
    return sync(collection=name, force=True, log=log)
