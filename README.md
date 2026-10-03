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

New machine, one command (Docker Desktop and Python 3.11+ first):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -LiqaHome E:\LIQA -AgencyHome E:\agency-agents
```

It installs pinned deps, writes `C:\LIQA-memory\loitkb.env` with a random Qdrant API key (readable only by you, never committed), starts Qdrant with the key, grants `liqa` ? `liqa:internal` + `jira:project:PF`, syncs every source, runs health and registers the monitor task (hourly + at logon; at startup when run elevated). Re-running is safe; the named volume keeps the data.

Paths default to sibling checkouts (`../LIQA`, `../agency-agents`), then `E:\`. Override with `LIQA_HOME`, `LIQA_AGENCY_HOME`, `LOITKB_DATA_DIR`.

Do not change the compose file to a Windows folder bind mount. Qdrant's memory-mapped storage was lost that way once; the health check fails if storage is not a Docker volume.

## Commands

| Command | Does |
| --- | --- |
| `sync [--source file\|flow\|memory\|workspace] [--force]` | Index only what changed; delete what vanished. Refuses to delete more than half the known docs (wrong `LIQA_HOME`) and alerts instead |
| `index-file PATH --phase P` | Index one LIQA run artifact now (LIQA calls this on every save) |
| `rebuild` | Back up, then drop and re-index everything (after corruption or schema change) |
| `search "q" [--as user]`, `ask "q" [--as user]` | Retrieve, or answer with citations |
| `remember "fact"` | Append to the memory journal and index it |
| `grant <user> <tags...>` | Write the access policy |
| `health` | Server, consistency, canary round trip, storage mount, disk, backup age + checksum + offsite, API key enforcement, schema version, source paths, query stats |
| `alerts` | Recent FAIL/WARN/recovery alerts (also in the Windows event log, source LOITKB, and the webhook if set) |
| `backup`, `backups`, `restore --from PATH [--into name] [--yes]` | Snapshot with checksum, list, restore. Restoring over the live collection needs `--yes`, a matching schema, and takes a backup first |
| `eval [--skip-quality]` | Full evaluator; writes `C:\LIQA-memory\reports\latest.md` |
| `monitor` | What the hourly task runs: sync, health, daily backup, daily eval |
| `jira [--scope JQL] [--full] [--attachments]`, `jira-reconcile`, `jira-grants` | Jira harvest |

## Evaluator

`py -3 -m loitkb eval` checks all 13 layers against the live system (scratch collections, a real restore, ACL denial, citation validation) and scores the golden set for dense, hybrid and hybrid+rerank. It prints `INDUSTRY-GRADE PASS` only when every layer passes. Thresholds: hit@5 â‰¥ 0.85, MRR@10 â‰¥ 0.65, abstain accuracy â‰¥ 0.8, false abstain â‰¤ 0.1, p95 â‰¤ 2500 ms, degraded â‰¤ 10%, no MRR regression > 0.05 against the previous run.

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

## Safety rails

- Secrets (Atlassian `ATATT` tokens, bearer tokens, `password=` / `api_key=` values, private keys) are redacted before anything is embedded.
- LIQA run files are tagged by the Jira key in their path (`PF-123` ? `jira:project:PF`), so a reader only sees projects it was granted.
- One writer per collection (OS file lock); writes stop below `LOITKB_HARD_MIN_FREE_GB` (default 1).
- Embedding model and dimensions are stored with the collection; a mismatch blocks writes until `rebuild`.
- Query log rotates at 20 MB, 3 files kept.

## Known limits

- Offsite backups are off until `LOITKB_OFFSITE_DIRS` points at another disk or share; health stays WARN until then.
- The golden set (40 answerable, 8 unanswerable) covers the LIQA corpus only. Jira content is not in it because Jira has not been harvested yet (no API token).
- Xray test steps need `XRAY_CLIENT_ID` / `XRAY_CLIENT_SECRET`; without them only the Jira issue fields are indexed.
- The embedding and reranker models are English-only.
- Jira issue-level security is not mirrored; access is per project.
- The reranker weights and abstain threshold were tuned on the same 48 questions they are scored on. Grow the golden set from real queries (`C:\LIQA-memory\logs\queries.jsonl`) before trusting small metric differences.
