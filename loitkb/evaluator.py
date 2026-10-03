"""Industry-grade evaluator.

Part 1 checks all 13 layers with live functional tests (scratch collections, real
models, real Qdrant). Part 2 measures retrieval quality on the golden set for
dense, hybrid and hybrid+rerank (hit@1, hit@5, MRR@10, nDCG@10, latency) plus
abstention on unanswerable questions, and compares against the previous run to
catch regressions. Writes JSON and Markdown reports; exit code 0 only if every
layer passes.
"""
from __future__ import annotations

import json
import math
import statistics
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import acl as acl_mod
from . import backup, health, manifest, store
from .config import settings
from .sources import Document

EVAL_GRANTS = ["liqa:internal"]
THRESHOLDS = {"hit@5": 0.85, "mrr@10": 0.65, "abstain_accuracy": 0.8, "false_abstain_rate": 0.1, "p95_ms": 2500, "regression_mrr": 0.05, "degraded_rate": 0.1}
LAYERS = [
    "vector_database", "local_embeddings", "stable_ids", "metadata_filters", "durable_storage_backups",
    "incremental_sync", "hybrid_search", "reranker", "document_aware_chunking", "permission_filtering",
    "evaluation_set", "monitoring_alerts", "answer_generation_citations",
]


def _scratch() -> str:
    name = settings().scratch_collection
    if store.client().collection_exists(name):
        store.client().delete_collection(name)
    manifest.clear(name)
    store.ensure_collection(name)
    return name


def _drop(name: str) -> None:
    if store.client().collection_exists(name):
        store.client().delete_collection(name)
    manifest.clear(name)


def _doc(doc_id: str, text: str, acl: list[str] | None = None, kind: str = "note", title: str = "Eval doc") -> Document:
    return Document(doc_id=doc_id, source_type="eval", title=title, kind="markdown", text=text, acl=acl or ["eval:a"], meta={"kind_label": kind})


# ---------------------------------------------------------------- golden set

def load_golden() -> list[dict]:
    path = settings().golden_set
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _relevant(hit: dict, expected: list[str]) -> bool:
    src = (hit.get("source") or hit.get("doc_id") or "").replace("\\", "/").lower()
    return any(e.replace("\\", "/").lower() in src for e in expected)


def quality(modes: tuple = (("dense", False), ("hybrid", False), ("hybrid", True))) -> dict:
    from .retrieve import retrieve

    golden = load_golden()
    answerable = [g for g in golden if g.get("expected_sources")]
    unanswerable = [g for g in golden if not g.get("expected_sources")]
    out = {}
    for mode, rr in modes:
        label = f"{mode}{'+rerank' if rr else ''}"
        hit1 = hit5 = mrr = ndcg = 0.0
        lat, misses, degraded = [], [], 0
        for g in answerable:
            res = retrieve(g["question"], "evaluator", grants=EVAL_GRANTS, limit=10, mode=mode, rerank=rr, log=False)
            lat.append(res["latency_ms"]["total"])
            degraded += 1 if res.get("degraded") else 0
            rels = [_relevant(h, g["expected_sources"]) for h in res["hits"][:10]]
            first = next((i for i, r in enumerate(rels) if r), None)
            hit1 += 1 if first == 0 else 0
            hit5 += 1 if first is not None and first < 5 else 0
            mrr += 1 / (first + 1) if first is not None else 0
            dcg = sum(1 / math.log2(i + 2) for i, r in enumerate(rels) if r)
            ideal = sum(1 / math.log2(i + 2) for i in range(min(sum(rels), 10))) or 1
            ndcg += dcg / ideal if sum(rels) else 0
            if first is None or first >= 5:
                misses.append({"id": g["id"], "question": g["question"], "got": [h["source"] for h in res["hits"][:3]]})
        n = max(1, len(answerable))
        lat.sort()
        out[label] = {
            "queries": len(answerable),
            "hit@1": round(hit1 / n, 3),
            "hit@5": round(hit5 / n, 3),
            "mrr@10": round(mrr / n, 3),
            "ndcg@10": round(ndcg / n, 3),
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            "degraded_rate": round(degraded / n, 3),
            "misses": misses,
        }
    # Abstention uses the production pipeline (hybrid + rerank).
    correct_abstain = sum(1 for g in unanswerable if retrieve(g["question"], "evaluator", grants=EVAL_GRANTS, log=False)["abstain"])
    false_abstain = sum(1 for g in answerable if retrieve(g["question"], "evaluator", grants=EVAL_GRANTS, log=False)["abstain"])
    out["abstention"] = {
        "unanswerable": len(unanswerable),
        "abstain_accuracy": round(correct_abstain / max(1, len(unanswerable)), 3),
        "false_abstain_rate": round(false_abstain / max(1, len(answerable)), 3),
    }
    return out


# ---------------------------------------------------------------- layer checks

def l_vector_database(ctx):
    import qdrant_client

    server = store.server_version()
    client_v = qdrant_client.__version__ if hasattr(qdrant_client, "__version__") else __import__("importlib.metadata").metadata.version("qdrant-client")
    info = store.client().get_collection(settings().collection)
    named = store.DENSE in info.config.params.vectors and store.SPARSE in (info.config.params.sparse_vectors or {})
    same = server.split(".")[:2] == str(client_v).split(".")[:2]
    return named and same, {"server": server, "client": client_v, "named_vectors": named, "points": info.points_count}


def l_local_embeddings(ctx):
    from .embed import embed_dense, embed_sparse

    cfg = settings()
    dims = len(embed_dense(["local embedding check"])[0])
    sparse_nnz = len(embed_sparse(["local embedding check PF-59486"])[0].indices)
    cache_ok = cfg.model_cache.exists() and any(cfg.model_cache.iterdir())
    remote_llm = bool(cfg.llm_base_url) and not any(h in cfg.llm_base_url for h in ("127.0.0.1", "localhost"))
    ok = dims == cfg.dense_size and sparse_nnz > 0 and cache_ok and not remote_llm
    return ok, {"dense_dims": dims, "sparse_terms": sparse_nnz, "model_cache": str(cfg.model_cache), "remote_llm_configured": remote_llm}


def l_stable_ids(ctx):
    from .sync import index_document

    name = ctx["scratch"]
    doc = _doc("eval:stable", "# Stable\n\nThis document is indexed twice and must not duplicate. " * 6)
    index_document(doc, name, force=True)
    first = store.count(name)
    ids_a = sorted(str(p.id) for p in store.client().scroll(name, limit=100)[0])
    index_document(doc, name, force=True)
    second = store.count(name)
    ids_b = sorted(str(p.id) for p in store.client().scroll(name, limit=100)[0])
    return first == second and ids_a == ids_b and first > 0, {"points_after_first": first, "points_after_second": second, "ids_identical": ids_a == ids_b}


def l_metadata_filters(ctx):
    from qdrant_client.models import FieldCondition, MatchValue

    from .sync import index_document

    name = ctx["scratch"]
    index_document(_doc("eval:kind-a", "# Alpha\n\nRecovery status filter test about write-off accounts.", kind="alpha"), name)
    index_document(_doc("eval:kind-b", "# Beta\n\nRecovery status filter test about write-off accounts.", kind="beta"), name)
    hits = store.search("recovery status write-off", ["eval:a"], must=[FieldCondition(key="kind", match=MatchValue(value="alpha"))], collection=name)
    kinds = {h["payload"]["kind"] for h in hits}
    return kinds == {"alpha"}, {"kinds_returned": sorted(kinds), "hits": len(hits)}


def l_durable_storage_backups(ctx):
    mount = health._check("storage_mount", health._storage_mount)
    meta = backup.latest()
    if not meta:
        return False, {"mount": mount, "backup": "none"}
    checksum = backup.verify(meta)
    created = datetime.strptime(meta["created_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    target = "loitkb_eval_restore"
    _drop(target)
    try:
        restored = backup.restore(meta, target_collection=target)
        restore_ok = restored["points"] == meta["points"]
    finally:
        _drop(target)
    ok = mount["status"] == "OK" and checksum and restore_ok and age_h <= settings().snapshot_max_age_hours
    return ok, {"mount": mount["detail"], "backup": meta["path"], "checksum_ok": checksum, "age_hours": round(age_h, 1),
                "restore_test_points": restored["points"], "backup_points": meta["points"], "offsite": meta.get("offsite", [])}


def l_incremental_sync(ctx):
    from .sync import sync_documents

    name = ctx["scratch"]
    a1 = _doc("eval:inc-a", "# A\n\nOriginal text with marker zebraoldmarker for incremental sync testing.")
    b = _doc("eval:inc-b", "# B\n\nDocument B will be removed from the source and must disappear.")
    s1 = sync_documents([a1, b], "eval-inc", name, log=lambda *_: None)
    a2 = _doc("eval:inc-a", "# A\n\nChanged text with marker giraffenewmarker for incremental sync testing.")
    s2 = sync_documents([a2], "eval-inc", name, log=lambda *_: None)
    s3 = sync_documents([a2], "eval-inc", name, log=lambda *_: None)
    texts = " ".join(p.payload.get("text", "") for p in store.client().scroll(name, limit=200)[0] if p.payload.get("source_type") == "eval")
    ok = (s1["indexed"] == 2 and s2["indexed"] == 1 and s2["deleted"] == 1 and s3["skipped"] == 1
          and "giraffenewmarker" in texts and "zebraoldmarker" not in texts and "must disappear" not in texts)
    return ok, {"first": s1, "change_and_delete": s2, "rerun": s3}


def l_hybrid_search(ctx):
    from .sync import index_document

    name = ctx["scratch"]
    index_document(_doc("eval:hyb-target", "# Ticket\n\nDefect PF-61234 recovery stage column is missing on the account inquiry modal."), name)
    for i in range(6):
        index_document(_doc(f"eval:hyb-noise-{i}", f"# Ticket {i}\n\nDefect PF-5{i}999 recovery stage column is missing on the account inquiry modal."), name)
    hybrid = store.search("PF-61234", ["eval:a"], mode="hybrid", limit=5, collection=name)
    dense = store.search("PF-61234", ["eval:a"], mode="dense", limit=5, collection=name)
    top_h = hybrid[0]["payload"]["doc_id"] if hybrid else None
    top_d = dense[0]["payload"]["doc_id"] if dense else None
    q = ctx.get("quality", {})
    better = q.get("hybrid", {}).get("mrr@10", 0) >= q.get("dense", {}).get("mrr@10", 0) - 0.01
    return top_h == "eval:hyb-target" and better, {"exact_id_top_hybrid": top_h, "exact_id_top_dense": top_d,
                                                   "golden_mrr_dense": q.get("dense", {}).get("mrr@10"), "golden_mrr_hybrid": q.get("hybrid", {}).get("mrr@10")}


def l_reranker(ctx):
    from .embed import rerank

    scores = rerank("How are Xray manual steps imported?", ["Import steps from CSV with Action, Data and Expected Result in the Xray wizard.", "The weather in Colombo is warm."])
    q = ctx.get("quality", {})
    base, rr = q.get("hybrid", {}), q.get("hybrid+rerank", {})
    not_worse = rr.get("mrr@10", 0) >= base.get("mrr@10", 0) - 0.02
    within_budget = (rr.get("p95_ms") or 1e9) <= THRESHOLDS["p95_ms"]
    rarely_degraded = rr.get("degraded_rate", 1) <= THRESHOLDS["degraded_rate"]
    return scores[0] > scores[1] and not_worse and within_budget and rarely_degraded, {
        "degraded_rate": rr.get("degraded_rate"),
        "relevant_vs_irrelevant": [round(s, 2) for s in scores], "mrr_hybrid": base.get("mrr@10"), "mrr_rerank": rr.get("mrr@10"),
        "mrr_gain": round(rr.get("mrr@10", 0) - base.get("mrr@10", 0), 3), "p95_ms": rr.get("p95_ms")}


def l_document_aware_chunking(ctx):
    cfg = settings()
    pts, offset, sizes, with_heading, md = [], None, [], 0, 0
    while True:
        batch, offset = store.client().scroll(cfg.collection, limit=1000, offset=offset, with_payload=["text", "heading", "source_type", "source"], with_vectors=False)
        pts.extend(batch)
        if offset is None:
            break
    for p in pts:
        text = p.payload.get("text", "")
        sizes.append(len(text))
        if p.payload.get("source_type") == "file" and str(p.payload.get("source", "")).lower().endswith((".md", ".mdc")):
            md += 1
            with_heading += 1 if p.payload.get("heading") else 0
    over = sum(1 for s in sizes if s > cfg.chunk_max_chars + cfg.chunk_min_chars)
    ratio = with_heading / max(1, md)
    ok = over == 0 and ratio >= 0.8 and bool(sizes)
    return ok, {"chunks": len(sizes), "max_chars": max(sizes or [0]), "mean_chars": round(statistics.fmean(sizes), 1) if sizes else 0,
                "oversized": over, "markdown_chunks_with_heading": round(ratio, 3)}


def l_permission_filtering(ctx):
    from .sync import index_document

    name = ctx["scratch"]
    index_document(_doc("eval:acl-pf", "# PF secret\n\nOnly PF project members may read the zebracrossing defect.", acl=["jira:project:PF"]), name)
    index_document(_doc("eval:acl-vv", "# VV secret\n\nOnly VV project members may read the zebracrossing defect.", acl=["jira:project:VV"]), name)
    pf = {h["payload"]["doc_id"] for h in store.search("zebracrossing defect", acl_mod.expand_tags(["jira:project:PF"])[-1:], collection=name)}
    wild = {h["payload"]["doc_id"] for h in store.search("zebracrossing defect", ["jira:*"], collection=name)}
    none = store.search("zebracrossing defect", [], collection=name)
    try:
        acl_mod.grants_for("nobody-unknown")
        denied = False
    except acl_mod.AccessDenied:
        denied = True
    ok = pf == {"eval:acl-pf"} and wild == {"eval:acl-pf", "eval:acl-vv"} and not none and denied
    return ok, {"pf_member_sees": sorted(pf), "jira_wildcard_sees": sorted(wild), "no_grants_sees": len(none), "unknown_principal_denied": denied}


def l_evaluation_set(ctx):
    golden = load_golden()
    q = ctx.get("quality", {})
    prod = q.get("hybrid+rerank", {})
    ab = q.get("abstention", {})
    ok = (len(golden) >= 30 and prod.get("hit@5", 0) >= THRESHOLDS["hit@5"] and prod.get("mrr@10", 0) >= THRESHOLDS["mrr@10"]
          and ab.get("abstain_accuracy", 0) >= THRESHOLDS["abstain_accuracy"] and ab.get("false_abstain_rate", 1) <= THRESHOLDS["false_abstain_rate"])
    prev = ctx.get("previous") or {}
    prev_mrr = (prev.get("quality", {}).get("hybrid+rerank") or {}).get("mrr@10")
    regression = prev_mrr is not None and prod.get("mrr@10", 0) < prev_mrr - THRESHOLDS["regression_mrr"]
    return ok and not regression, {"golden_questions": len(golden), "production": {k: prod.get(k) for k in ("hit@1", "hit@5", "mrr@10", "ndcg@10", "p95_ms")},
                                   "abstention": ab, "thresholds": THRESHOLDS, "previous_mrr": prev_mrr, "regression": regression}


def l_monitoring_alerts(ctx):
    report = health.run(write=True, alert=False)
    by = {c["check"]: c["status"] for c in report["checks"]}
    task = subprocess.run(["schtasks", "/Query", "/TN", "LOITKB Monitor"], capture_output=True, text=True)
    log_ok = settings().query_log.exists()
    ok = report["overall"] != "FAIL" and by.get("canary_roundtrip") == "OK" and task.returncode == 0 and log_ok
    return ok, {"health": report["overall"], "checks": by, "scheduled_task": task.returncode == 0, "query_log": str(settings().query_log)}


def l_answer_generation_citations(ctx):
    from .answer import ask

    golden = load_golden()
    answerable = [g for g in golden if g.get("expected_sources")][:6]
    unanswerable = [g for g in golden if not g.get("expected_sources")][:3]
    acl_mod.save_grants("evaluator", EVAL_GRANTS)
    rows, ok = [], True
    for g in answerable:
        a = ask(g["question"], "evaluator")
        good = not a["abstained"] and a["citation_check"]["valid"]
        ok &= good
        rows.append({"q": g["id"], "abstained": a["abstained"], "citations_valid": a["citation_check"]["valid"], "problems": a["citation_check"]["problems"][:2]})
    for g in unanswerable:
        a = ask(g["question"], "evaluator")
        ok &= a["abstained"]
        rows.append({"q": g["id"], "expected_abstain": True, "abstained": a["abstained"]})
    return ok, {"samples": rows}


CHECKS = {name: globals()[f"l_{name}"] for name in LAYERS}


def run(skip_quality: bool = False) -> dict:
    cfg = settings()
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    acl_mod.save_grants("evaluator", EVAL_GRANTS)
    previous_files = sorted(cfg.reports_dir.glob("eval-*.json"))
    previous = json.loads(previous_files[-1].read_text(encoding="utf-8")) if previous_files else None
    started = time.time()
    ctx: dict = {"previous": previous}
    ctx["quality"] = {} if skip_quality else quality()
    ctx["scratch"] = _scratch()
    layers = []
    try:
        for name in LAYERS:
            t = time.perf_counter()
            try:
                ok, evidence = CHECKS[name](ctx)
                status = "PASS" if ok else "FAIL"
            except Exception as exc:
                status, evidence = "FAIL", {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
            layers.append({"layer": name, "status": status, "seconds": round(time.perf_counter() - t, 1), "evidence": evidence})
    finally:
        _drop(ctx["scratch"])
    passed = sum(1 for l in layers if l["status"] == "PASS")
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "verdict": "INDUSTRY-GRADE PASS" if passed == len(layers) else "FAIL",
        "passed": passed,
        "total": len(layers),
        "seconds": round(time.time() - started, 1),
        "layers": layers,
        "quality": ctx["quality"],
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (cfg.reports_dir / f"eval-{stamp}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (cfg.reports_dir / f"eval-{stamp}.md").write_text(markdown(report), encoding="utf-8")
    (cfg.reports_dir / "latest.md").write_text(markdown(report), encoding="utf-8")
    manifest.set_state("last_eval", report["at"])
    if report["verdict"] != "INDUSTRY-GRADE PASS":
        health.send_alert("FAIL", f"LOITKB evaluator FAIL: {passed}/{len(layers)} layers")
    return report


def markdown(report: dict) -> str:
    lines = [f"# LOITKB evaluator â€” {report['verdict']}", "", f"Run at {report['at']} Â· {report['passed']}/{report['total']} layers Â· {report['seconds']} s", "",
             "| Layer | Status | Key evidence |", "| --- | --- | --- |"]
    for l in report["layers"]:
        ev = {k: v for k, v in (l["evidence"] or {}).items() if k not in ("trace", "samples", "misses")}
        lines.append(f"| {l['layer']} | {l['status']} | {json.dumps(ev, default=str)[:300].replace('|', '/')} |")
    q = report.get("quality") or {}
    if q:
        lines += ["", "## Retrieval quality (golden set)", "", "| Pipeline | hit@1 | hit@5 | MRR@10 | nDCG@10 | p50 ms | p95 ms | degraded |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for label, m in q.items():
            if label == "abstention":
                continue
            lines.append(f"| {label} | {m['hit@1']} | {m['hit@5']} | {m['mrr@10']} | {m['ndcg@10']} | {m['p50_ms']} | {m['p95_ms']} | {m.get('degraded_rate', 0)} |")
        ab = q.get("abstention", {})
        lines += ["", f"Abstention: accuracy {ab.get('abstain_accuracy')} on {ab.get('unanswerable')} unanswerable questions; false-abstain rate {ab.get('false_abstain_rate')}."]
        misses = (q.get("hybrid+rerank") or {}).get("misses") or []
        if misses:
            lines += ["", "## Misses (production pipeline)", ""] + [f"- {m['id']}: {m['question']} â†’ got {m['got']}" for m in misses]
    return "\n".join(lines) + "\n"
