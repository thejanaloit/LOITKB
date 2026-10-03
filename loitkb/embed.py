"""Local models only. Nothing leaves the machine to embed or rerank."""
from __future__ import annotations

from functools import lru_cache

from .config import settings


def _disable_onnx_spinning() -> None:
    """Three ONNX sessions (dense, sparse, rerank) run back to back per query. With
    spin-waiting on, idle pools keep burning cores and the next model runs ~2-4x
    slower. fastembed builds its own SessionOptions and exposes no spinning knob,
    so wrap the constructor before any model loads."""
    import onnxruntime as ort

    if getattr(ort.SessionOptions, "_loitkb_nospin", False):
        return
    original = ort.SessionOptions

    def factory():
        so = original()
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        so.add_session_config_entry("session.inter_op.allow_spinning", "0")
        return so

    factory._loitkb_nospin = True
    ort.SessionOptions = factory


_disable_onnx_spinning()


@lru_cache(maxsize=1)
def dense_model():
    from fastembed import TextEmbedding

    cfg = settings()
    cfg.model_cache.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(cfg.dense_model, cache_dir=str(cfg.model_cache))


@lru_cache(maxsize=1)
def sparse_model():
    from fastembed import SparseTextEmbedding

    cfg = settings()
    cfg.model_cache.mkdir(parents=True, exist_ok=True)
    return SparseTextEmbedding(cfg.sparse_model, cache_dir=str(cfg.model_cache))


@lru_cache(maxsize=1)
def rerank_model():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    cfg = settings()
    cfg.model_cache.mkdir(parents=True, exist_ok=True)
    return TextCrossEncoder(cfg.rerank_model, cache_dir=str(cfg.model_cache), threads=cfg.rerank_threads or None)


def embed_dense(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in dense_model().embed(texts, batch_size=32)]


def embed_dense_query(text: str) -> list[float]:
    return next(iter(dense_model().query_embed([text]))).tolist()


def embed_sparse(texts: list[str]):
    from qdrant_client.models import SparseVector

    return [SparseVector(indices=e.indices.tolist(), values=e.values.tolist()) for e in sparse_model().embed(texts, batch_size=64)]


def embed_sparse_query(text: str):
    from qdrant_client.models import SparseVector

    e = next(iter(sparse_model().query_embed(text)))
    return SparseVector(indices=e.indices.tolist(), values=e.values.tolist())


def rerank(query: str, docs: list[str]) -> list[float]:
    if not docs:
        return []
    cut = settings().rerank_max_chars
    return [float(s) for s in rerank_model().rerank(query, [d[:cut] for d in docs], batch_size=64)]
