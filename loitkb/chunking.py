"""Structure-aware chunking.

Markdown splits on headings first and keeps the heading path as metadata. Long
sections split on paragraphs, then sentences, never mid-word. Python splits on
top-level def/class blocks. Every chunk carries a short context prefix (title and
headings) that is embedded with it, so a chunk that only says "Click Import" still
knows it belongs to "Xray UI RPA > Proven wizard".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*\[(])")
_FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


@dataclass
class Chunk:
    text: str
    index: int
    heading: str = ""
    context: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def embed_text(self) -> str:
        return f"{self.context}\n{self.text}" if self.context else self.text


def _split_long(text: str, max_chars: int) -> list[str]:
    """Paragraphs, then sentences, then words. Never cuts inside a word."""
    if len(text) <= max_chars:
        return [text]
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
            continue
        lines = para.split("\n")
        if len(lines) > 1 and all(len(line) <= max_chars for line in lines):
            units.extend(line for line in lines if line.strip())
            continue
        for sentence in _SENTENCE.split(para):
            if len(sentence) <= max_chars:
                units.append(sentence)
                continue
            words, buf = sentence.split(), ""
            for word in words:
                if len(buf) + len(word) + 1 > max_chars and buf:
                    units.append(buf)
                    buf = word
                else:
                    buf = f"{buf} {word}".strip()
            if buf:
                units.append(buf)
    return units


def _pack(units: list[str], max_chars: int, overlap: int) -> list[str]:
    """Greedy pack units up to max_chars, carrying a tail of whole units as overlap."""
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for unit in units:
        add = len(unit) + (2 if cur else 0)
        if cur and size + add > max_chars:
            out.append("\n\n".join(cur))
            tail: list[str] = []
            tail_size = 0
            for prev in reversed(cur):
                if tail_size + len(prev) > overlap:
                    break
                tail.insert(0, prev)
                tail_size += len(prev) + 2
            if tail_size + len(unit) > max_chars:
                tail, tail_size = [], 0
            cur, size = tail, tail_size
            add = len(unit) + (2 if cur else 0)
        cur.append(unit)
        size += add
    if cur:
        out.append("\n\n".join(cur))
    return out


def chunk_markdown(text: str, title: str, max_chars: int = 900, overlap: int = 150, min_chars: int = 60) -> list[Chunk]:
    text = _FRONT_MATTER.sub("", text.replace("\r\n", "\n"))
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = []
    body: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else _HEADING.match(line)
        if match:
            if any(b.strip() for b in body):
                sections.append((list(path), body))
            level = len(match.group(1))
            path = path[: level - 1] + [match.group(2).strip()]
            body = []
        else:
            body.append(line)
    if any(b.strip() for b in body):
        sections.append((list(path), body))

    chunks: list[Chunk] = []
    for heading_path, lines in sections:
        section = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if not section:
            continue
        heading = " > ".join(heading_path)
        tail = heading_path[1:] if heading_path and heading_path[0].strip() == title.strip() else heading_path
        context = " > ".join([title, *tail])
        for piece in _pack(_split_long(section, max_chars), max_chars, overlap):
            if len(piece) < min_chars and chunks and chunks[-1].heading == heading:
                chunks[-1].text += "\n\n" + piece
                continue
            if len(piece) < min_chars and not heading:
                continue
            chunks.append(Chunk(text=piece, index=len(chunks), heading=heading, context=context))
    return chunks


def chunk_python(text: str, title: str, max_chars: int = 900, overlap: int = 150) -> list[Chunk]:
    blocks: list[tuple[str, list[str]]] = []
    name, cur = "module", []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = re.match(r"^(def|class|async def)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if match and cur:
            blocks.append((name, cur))
            name, cur = match.group(2), []
        elif match:
            name = match.group(2)
        cur.append(line)
    if cur:
        blocks.append((name, cur))
    chunks: list[Chunk] = []
    for block_name, lines in blocks:
        block = "\n".join(lines).strip()
        if len(block) < 40:
            continue
        for piece in _pack(_split_long(block, max_chars), max_chars, overlap):
            chunks.append(Chunk(text=piece, index=len(chunks), heading=block_name, context=f"{title} > {block_name}"))
    return chunks


def chunk_plain(text: str, title: str, max_chars: int = 900, overlap: int = 150, min_chars: int = 20) -> list[Chunk]:
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    if len(text) < min_chars:
        return []
    return [
        Chunk(text=piece, index=i, context=title)
        for i, piece in enumerate(_pack(_split_long(text, max_chars), max_chars, overlap))
    ]


def chunk_document(text: str, title: str, kind: str, max_chars: int = 900, overlap: int = 150, min_chars: int = 60) -> list[Chunk]:
    if kind in ("markdown", "md", "mdc"):
        return chunk_markdown(text, title, max_chars, overlap, min_chars)
    if kind in ("python", "py"):
        return chunk_python(text, title, max_chars, overlap)
    return chunk_plain(text, title, max_chars, overlap)
