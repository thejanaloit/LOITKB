"""Document sources. The vector store is derived data: every source can be replayed."""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .config import MAX_FILE_BYTES, SKIP_PARTS, settings


@dataclass
class Document:
    doc_id: str
    source_type: str
    title: str
    kind: str
    text: str
    acl: list[str]
    meta: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        body = json.dumps([self.text, self.title, sorted(self.acl), self.meta], sort_keys=True, default=str)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _kind(path: Path) -> str:
    return {".md": "markdown", ".mdc": "markdown", ".py": "python"}.get(path.suffix.lower(), "text")


def _title(path: Path, text: str) -> str:
    for line in text.splitlines()[:30]:
        if line.startswith("# "):
            return line[2:].strip()
        if line.lower().startswith("description:"):
            return f"{path.stem}: {line.split(':', 1)[1].strip().strip(chr(39)).strip(chr(34))[:120]}"
    return path.stem


def iter_files() -> Iterator[Document]:
    seen: set[str] = set()
    for root, patterns, acl in settings().file_sources():
        if not root.exists():
            continue
        for pattern in patterns:
            for path in sorted(root.glob(pattern)):
                key = str(path.resolve()).lower()
                if key in seen or not path.is_file():
                    continue
                if any(part.lower() in key for part in SKIP_PARTS):
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                seen.add(key)
                text = path.read_text(encoding="utf-8", errors="replace")
                yield Document(
                    doc_id=f"file:{path.resolve()}",
                    source_type="file",
                    title=_title(path, text),
                    kind=_kind(path),
                    text=text,
                    acl=[acl],
                    meta={"place": str(path), "source": str(path), "kind_label": "source", "phase": "knowledge"},
                )


def iter_flow() -> Iterator[Document]:
    """LIQA ISTQB phases from mcp/laws.py, one document per phase."""
    mcp_dir = settings().liqa_home / "mcp"
    if str(mcp_dir) not in sys.path:
        sys.path.insert(0, str(mcp_dir))
    try:
        from laws import ISTQB_PHASES  # type: ignore
    except Exception:
        return
    for phase in ISTQB_PHASES:
        steps = "\n".join(f"{n}. {step}" for n, step in enumerate(phase.get("do", []), start=1))
        text = f"# LIQA phase {phase['id']} {phase['title']}\n\nISTQB activity: {phase.get('istqb', '')}\n\n## Steps\n\n{steps}\n"
        yield Document(
            doc_id=f"flow:phase:{phase['key']}",
            source_type="flow",
            title=f"LIQA phase {phase['id']} {phase['title']}",
            kind="markdown",
            text=text,
            acl=["liqa:internal"],
            meta={"place": f"phase-{phase['id']}", "source": "mcp/laws.py", "kind_label": "flow_step", "phase": phase["key"]},
        )


def iter_memories() -> Iterator[Document]:
    journal = settings().memory_journal
    if not journal.exists():
        return
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        yield Document(
            doc_id=row["doc_id"],
            source_type="memory",
            title=f"LIQA memory {row.get('task_key') or ''}".strip(),
            kind="text",
            text=row["text"],
            acl=row.get("acl") or ["liqa:internal"],
            meta={
                "place": row.get("place", "liqa_memory_remember"),
                "source": row.get("task_key") or "runtime",
                "kind_label": row.get("kind", "memory"),
                "phase": row.get("phase", "completion"),
                "task_key": row.get("task_key", ""),
                "remembered_at": row.get("at", ""),
            },
        )


def journal_memory(text: str, kind: str, phase: str, task_key: str, place: str, acl: list[str] | None = None) -> Document:
    """Append-only system of record for runtime memories, so a rebuild never loses them."""
    from .store import now

    body = text.strip()
    digest = hashlib.sha256(f"{task_key}\n{body}".encode("utf-8")).hexdigest()[:20]
    doc_id = f"mem:{task_key or 'runtime'}:{digest}"
    journal = settings().memory_journal
    journal.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if journal.exists():
        existing = {json.loads(l)["doc_id"] for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()}
    row = {"doc_id": doc_id, "text": body, "kind": kind, "phase": phase, "task_key": task_key, "place": place, "acl": acl or ["liqa:internal"], "at": now()}
    if doc_id not in existing:
        with journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return next(d for d in iter_memories() if d.doc_id == doc_id)


SOURCES = {"file": iter_files, "flow": iter_flow, "memory": iter_memories}
