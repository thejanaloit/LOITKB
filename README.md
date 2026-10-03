# LOITKB

Local, permission-aware hybrid RAG for LIQA. Everything runs on the company machine: Qdrant for storage, local ONNX models for embeddings and reranking, an extractive answerer with checked citations. No document text leaves the machine unless you point the optional answer LLM at a remote endpoint.

GitHub holds the code only. The vector store, the manifest, the memory journal, the access policy and every Jira credential stay under `C:\LIQA-memory` (override with `LOITKB_DATA_DIR`).

## Architecture

```
sources (LIQA docs/skills/packaging, flow phases, memory journal, Jira)
  -> structure-aware chunking (heading path kept, never cuts a word)
  -> dense bge-small-en-v1.5 + sparse BM25 (local)
  -> Qdrant 1.19.1, named vectors, keyword payload indexes, ACL tags
query
  -> ACL filter inside Qdrant (deny by default)
  -> hybrid search (dense + BM25, RRF)
  -> cross-encoder rerank of the top 8, blended with hybrid rank
  -> abstain below a calibrated score
  -> answer with [n] citations, each checked against its chunk
```

| Layer | Where |
| --- | --- |
| Vector DB, named dense + sparse vectors, HNSW, payload indexes | `loitkb/store.py` |
| Local embeddings and reranker | `loitkb/embed.py` |
| Stable ids `uuid(sha256(doc_id#chunk))` | `loitkb/store.py` |
| Structure-aware chunking | `loitkb/chunking.py` |
| Incremental sync, replace on change, delete when gone | `loitkb/sync.py`, `loitkb/manifest.py` |
| Permission filtering | `loitkb/acl.py` |
| Retrieval pipeline, latency budget, abstention | `loitkb/retrieve.py` |
| Answers with validated citations | `loitkb/answer.py` |
| Snapshots, verify, rotate, restore | `loitkb/backup.py` |
| Health checks and alert webhook | `loitkb/health.py` |
| Jira connector (incremental, comments, attachments, reconcile, grants) | `loitkb/jira.py` |
| Evaluator (13 layers + golden-set metrics) | `loitkb/evaluator.py`, `eval/golden.jsonl` |

## Run

```powershell
docker compose up -d
py -3 -m pip install -r requirements.txt
py -3 -m loitkb grant <your-windows-username> liqa:internal
py -3 -m loitkb sync
py -3 -m loitkb ask "How are Xray manual steps imported?"
powershell -ExecutionPolicy Bypass -File scripts\install-monitor-task.ps1
```

Do not change the compose file to a Windows folder bind mount. Qdrant's memory-mapped storage was lost that way once; the health check fails if storage is not a Docker volume.

## Commands

| Command | Does |
| --- | --- |
| `sync [--source file\|flow\|memory] [--force]` | Index only what changed; delete what vanished |
| `rebuild` | Drop and re-index everything (after corruption or schema change) |
| `search "q" [--as user]`, `ask "q" [--as user]` | Retrieve, or answer with citations |
| `remember "fact"` | Append to the memory journal and index it |
| `grant <user> <tags...>` | Write the access policy |
| `health` | Server, consistency, canary round trip, storage mount, disk, backup age, query stats |
| `backup`, `backups`, `restore [--into name]` | Snapshot with checksum, list, restore |
| `eval [--skip-quality]` | Full evaluator; writes `C:\LIQA-memory\reports\latest.md` |
| `monitor` | What the hourly task runs: sync, health, daily backup, daily eval |
| `jira [--scope JQL] [--full] [--attachments]`, `jira-reconcile`, `jira-grants` | Jira harvest |

## Evaluator

`py -3 -m loitkb eval` checks all 13 layers against the live system (scratch collections, a real restore, ACL denial, citation validation) and scores the golden set for dense, hybrid and hybrid+rerank. It prints `INDUSTRY-GRADE PASS` only when every layer passes. Thresholds: hit@5 ≥ 0.85, MRR@10 ≥ 0.65, abstain accuracy ≥ 0.8, false abstain ≤ 0.1, p95 ≤ 2500 ms, degraded ≤ 10%, no MRR regression > 0.05 against the previous run.

Result on 3 Oct 2026, 994 chunks: 13/13, hybrid+rerank hit@1 0.775, hit@5 0.95, MRR@10 0.858, nDCG@10 0.866, p95 1287 ms, abstain accuracy 1.0, false abstain 0.05.

## Jira

Jira Cloud needs an API token; the account password returns 401. Save it only in `C:\LIQA-memory\jira.env`:

```
JIRA_BASE_URL=https://lolcgroupdev.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=your-token
```

`jira-grants` derives each person's project grants from their own token, so retrieval only shows projects that person can browse.

## LIQA

`E:\LIQA\mcp\memory_rag.py` is a thin adapter over this package (`LOITKB_HOME`, default `C:\src\LOITKB`, principal `liqa`). `liqa_boot` reports `memory_status`; when the memory is unreachable every memory call returns `status: DOWN` with the fix instead of an empty result.

## Known limits

- The golden set (40 answerable, 8 unanswerable) covers the LIQA corpus only. Jira content is not in it because Jira has not been harvested yet (no API token).
- Xray test steps need `XRAY_CLIENT_ID` / `XRAY_CLIENT_SECRET`; without them only the Jira issue fields are indexed.
- The embedding and reranker models are English-only.
- Jira issue-level security is not mirrored; access is per project.
- The reranker weights and abstain threshold were tuned on the same 48 questions they are scored on. Grow the golden set from real queries (`C:\LIQA-memory\logs\queries.jsonl`) before trusting small metric differences.
