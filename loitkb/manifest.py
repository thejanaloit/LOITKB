"""SQLite manifest: which documents are indexed, with which content hash and chunk count."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    collection TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunks INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (collection, doc_id)
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Path | None = None):
    path = path or settings().manifest_db
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_all(collection: str, source_type: str | None = None, path: Path | None = None) -> dict[str, tuple[str, int]]:
    with connect(path) as conn:
        if source_type:
            rows = conn.execute("SELECT doc_id, content_hash, chunks FROM docs WHERE collection=? AND source_type=?", (collection, source_type))
        else:
            rows = conn.execute("SELECT doc_id, content_hash, chunks FROM docs WHERE collection=?", (collection,))
        return {r[0]: (r[1], r[2]) for r in rows}


def put(collection: str, doc_id: str, source_type: str, content_hash: str, chunks: int, at: str, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?)",
            (collection, doc_id, source_type, content_hash, chunks, at),
        )


def remove(collection: str, doc_id: str, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("DELETE FROM docs WHERE collection=? AND doc_id=?", (collection, doc_id))


def total_chunks(collection: str, path: Path | None = None) -> int:
    with connect(path) as conn:
        return int(conn.execute("SELECT COALESCE(SUM(chunks),0) FROM docs WHERE collection=?", (collection,)).fetchone()[0])


def clear(collection: str, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("DELETE FROM docs WHERE collection=?", (collection,))


def get_state(key: str, default: str = "", path: Path | None = None) -> str:
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_state(key: str, value: str, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))
