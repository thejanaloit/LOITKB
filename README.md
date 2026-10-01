# LOITKB

Durable memory for LIQA. Qdrant stores the vectors on disk. A local embedding model turns each piece of the LIQA flow into a vector. LIQA recalls that memory at boot, planning, and learn, and writes new lessons back at the end of a run.

The vector database is not in this repository. GitHub holds the system. The vectors stay in `data/qdrant` on the machine that runs it. Copy that folder when you need a backup.

## Run

```powershell
docker compose up -d
py -3 -m pip install -r requirements.txt
$env:LIQA_HOME = "E:\LIQA"
$env:LIQA_MEMORY_DIR = "$pwd\data"
py -3 build_memory.py
```

Qdrant listens on `127.0.0.1:6333`. The collection name is `liqa_memory`.

## What gets stored

- Each ISTQB phase step from LIQA
- The six live hooks: boot, learn speed, learn record, learn cycle, recall, remember
- Source text from the LIQA repo (docs, mcp, skills, packaging, company, apps) and the agency rules, when `LIQA_HOME` points at that repo

Secrets, credential files, and captures are skipped.

## Jira organization harvest

This login can see the Jira projects it is allowed to open. On 1 Oct 2026 the connected LOLC login could see four projects (FXN, MDP, PF, VV) and 83,672 issues. It cannot see projects that account cannot browse.

Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens and save it only in `C:\LIQA-memory\jira.env`:

```
JIRA_BASE_URL=https://lolcgroupdev.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=your-token
```

Then:

```powershell
py -3 jira_ingest.py
```

The script stores users, stories, development issues, QA issues, test cases, descriptions, and comments in the same Qdrant collection. It resumes from `jira-checkpoint.json` if it stops. The account password is not accepted by Jira Cloud.

## LIQA

The same memory is wired into LIQA as `mcp/memory_rag.py`. These tools call it:

- `liqa_boot`
- `liqa_learn_speed`
- `liqa_learn_record`
- `liqa_learn_cycle`
- `liqa_memory_recall`
- `liqa_memory_remember`
- `liqa_memory_status`
