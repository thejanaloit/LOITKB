"""Answer generation with citations.

Default provider is extractive and fully local: it selects the sentences from the
retrieved chunks that best match the question and cites each one as [n]. An
OpenAI-compatible provider (Azure OpenAI, a local Ollama at http://127.0.0.1:11434/v1)
can be enabled with LOITKB_LLM_BASE_URL / LOITKB_LLM_MODEL. Whatever the provider,
the answer is validated: every citation must point at a retrieved chunk, every
sentence must carry a citation, and each sentence must overlap its cited chunk.
Below the confidence threshold the system abstains instead of guessing.
"""
from __future__ import annotations

import re
from typing import Any

from .config import settings
from .retrieve import retrieve

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-|]*")
_STOP = set(
    "a an the and or of to in on for with is are was were be by as at it this that from how what which who when where why do does did "
    "i we you they he she my our your their can should must will would not no yes if then than into about".split()
)
ABSTAIN_TEXT = "I could not find this in the knowledge base you have access to."


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP and len(w) > 1}


def _sentences(text: str) -> list[str]:
    text = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    out = []
    for p in parts:
        p = re.sub(r"^[\s#>*\-|0-9.)]+", "", p)
        p = re.sub(r"^\[[ xX]\]\s*", "", p).strip()
        if len(p) > 25:
            out.append(p)
    return out


def _extractive(question: str, hits: list[dict], max_sentences: int = 4) -> str:
    q = _terms(question)
    scored = []
    for n, hit in enumerate(hits, start=1):
        for pos, sentence in enumerate(_sentences(hit["text"])):
            overlap = len(q & _terms(sentence))
            if overlap == 0:
                continue
            scored.append((overlap + 1.0 / n - 0.01 * pos, n, sentence))
    scored.sort(key=lambda s: s[0], reverse=True)
    chosen, used = [], set()
    for _, n, sentence in scored:
        if sentence in used:
            continue
        used.add(sentence)
        chosen.append(f"{sentence.rstrip('.')}. [{n}]")
        if len(chosen) >= max_sentences:
            break
    if not chosen and hits:
        first = _sentences(hits[0]["text"])
        chosen = [f"{first[0].rstrip('.')}. [1]"] if first else []
    return " ".join(chosen)


def _llm(question: str, hits: list[dict]) -> str:
    import requests

    cfg = settings()
    context = "\n\n".join(f"[{n}] ({h.get('title')} > {h.get('heading') or ''})\n{h['text']}" for n, h in enumerate(hits, start=1))
    body = {
        "model": cfg.llm_model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": "Answer only from the numbered context. End every sentence with its source as [n]. "
                "If the context does not contain the answer, reply exactly: " + ABSTAIN_TEXT,
            },
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
        ],
    }
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}"} if cfg.llm_api_key else {}
    resp = requests.post(cfg.llm_base_url.rstrip("/") + "/chat/completions", json=body, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def validate_citations(answer: str, hits: list[dict], min_overlap: float = 0.3) -> dict[str, Any]:
    if answer.strip() == ABSTAIN_TEXT:
        return {"valid": True, "abstained": True, "problems": []}
    problems = []
    sentences = [s for s in re.split(r"(?<=\[\d\])\s+|(?<=\[\d\d\])\s+", answer.strip()) if s.strip()]
    for s in sentences:
        cites = [int(c) for c in re.findall(r"\[(\d+)\]", s)]
        if not cites:
            problems.append({"sentence": s, "problem": "no citation"})
            continue
        for c in cites:
            if c < 1 or c > len(hits):
                problems.append({"sentence": s, "problem": f"citation [{c}] does not exist"})
                continue
            claim = _terms(re.sub(r"\[\d+\]", "", s))
            support = _terms(hits[c - 1]["text"])
            ratio = len(claim & support) / max(1, len(claim))
            if ratio < min_overlap:
                problems.append({"sentence": s, "problem": f"weak support from [{c}] ({ratio:.2f})"})
    return {"valid": not problems, "abstained": False, "problems": problems, "sentences": len(sentences)}


def ask(question: str, principal: str, *, limit: int = 5, provider: str | None = None, collection: str | None = None) -> dict[str, Any]:
    cfg = settings()
    provider = provider or ("llm" if cfg.llm_base_url and cfg.llm_model else "extractive")
    found = retrieve(question, principal, limit=limit, collection=collection)
    hits = found["hits"]
    if found["abstain"] or not hits:
        answer = ABSTAIN_TEXT
    elif provider == "llm":
        answer = _llm(question, hits)
    else:
        answer = _extractive(question, hits)
    check = validate_citations(answer, hits)
    return {
        "ok": True,
        "question": question,
        "provider": provider,
        "answer": answer,
        "abstained": answer == ABSTAIN_TEXT,
        "citations": [
            {"n": h["rank"], "title": h["title"], "heading": h["heading"], "source": h["source"], "doc_id": h["doc_id"], "chunk": h["chunk_index"], "score": h["score"]}
            for h in hits
        ],
        "citation_check": check,
        "latency_ms": found["latency_ms"],
    }
