"""Jira Cloud connector.

- One document per issue (doc_id jira:<KEY>). An edited issue replaces its old
  chunks; it never duplicates them.
- Incremental: after the first full pass, only issues updated since the last
  successful run (minus a safety window) are fetched.
- reconcile(): lists every visible key and deletes issues that were removed or
  moved out of reach.
- ACL: every chunk is tagged jira:project:<KEY>. grants_from_jira() derives a
  person's grants from the projects their own token can browse.
- Comments: full thread (paged) when the search response is truncated.
- Attachments: text from PDF, DOCX, TXT/CSV/MD up to a size cap (opt-in).
- Xray: test steps (Action | Data | Expected Result) when XRAY_CLIENT_ID and
  XRAY_CLIENT_SECRET exist. Without keys this part is skipped and reported.

Credentials come from the environment or <data dir>/jira.env. Jira Cloud rejects
account passwords; use an API token from id.atlassian.com.
"""
from __future__ import annotations

import io
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

from . import manifest
from .config import settings
from .sources import Document

FIELDS = ["summary", "description", "comment", "issuetype", "status", "project", "assignee", "reporter",
          "creator", "created", "updated", "labels", "parent", "priority", "fixVersions", "components",
          "issuelinks", "attachment", "resolution"]
ATTACH_EXT = (".pdf", ".docx", ".txt", ".csv", ".md")
ATTACH_MAX_BYTES = 8_000_000


def _load_env() -> None:
    path = settings().jira_env
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


class JiraClient:
    def __init__(self, base: str | None = None, email: str | None = None, token: str | None = None):
        _load_env()
        self.base = (base or os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
        self.email = email or os.environ.get("JIRA_EMAIL") or ""
        token = token or os.environ.get("JIRA_API_TOKEN") or ""
        if not (self.base and self.email and token):
            raise SystemExit(f"Missing JIRA_BASE_URL, JIRA_EMAIL or JIRA_API_TOKEN (env or {settings().jira_env}).")
        self.session = requests.Session()
        self.session.auth = (self.email, token)
        self.session.headers.update({"Accept": "application/json"})

    def call(self, method: str, path: str, **kw) -> requests.Response:
        url = path if path.startswith("http") else self.base + path
        for attempt in range(6):
            resp = self.session.request(method, url, timeout=120, **kw)
            if resp.status_code in (429, 502, 503, 504):
                time.sleep(float(resp.headers.get("Retry-After") or 2 ** attempt))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Jira HTTP {resp.status_code} on {path}: {resp.text[:300]}")
            return resp
        raise RuntimeError(f"Jira kept throttling {path}")

    def json(self, method: str, path: str, **kw):
        return self.call(method, path, **kw).json()

    def myself(self) -> dict:
        return self.json("GET", "/rest/api/3/myself")

    def search(self, jql: str, fields: list[str]) -> Iterator[dict]:
        token = None
        while True:
            body = {"jql": jql, "maxResults": 100, "fields": fields}
            if token:
                body["nextPageToken"] = token
            page = self.json("POST", "/rest/api/3/search/jql", json=body)
            yield from page.get("issues") or []
            token = page.get("nextPageToken")
            if page.get("isLast") or not token:
                return

    def all_comments(self, key: str) -> list[dict]:
        out, start = [], 0
        while True:
            page = self.json("GET", f"/rest/api/3/issue/{key}/comment?startAt={start}&maxResults=100")
            out.extend(page.get("comments") or [])
            start += len(page.get("comments") or [])
            if start >= int(page.get("total") or 0) or not page.get("comments"):
                return out

    def browsable_projects(self) -> list[str]:
        keys, start = [], 0
        while True:
            page = self.json("GET", f"/rest/api/3/project/search?startAt={start}&maxResults=50&action=browse")
            keys.extend(p["key"] for p in page.get("values") or [])
            start += len(page.get("values") or [])
            if page.get("isLast", True):
                return keys


def adf_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "text":
            return node.get("text", "")
        if kind in ("hardBreak",):
            return "\n"
        if kind == "mention":
            return (node.get("attrs") or {}).get("text", "")
        inner = adf_text(node.get("content"))
        if kind in ("paragraph", "heading", "listItem", "tableRow", "codeBlock", "blockquote"):
            return inner.strip() + "\n"
        if kind in ("tableCell", "tableHeader"):
            return inner.strip() + " | "
        return inner
    return ""


def _name(value) -> str:
    return str(value.get("displayName") or value.get("name") or value.get("key") or "") if isinstance(value, dict) else ""


def attachment_text(client: JiraClient, att: dict) -> str:
    name = (att.get("filename") or "").lower()
    if not name.endswith(ATTACH_EXT) or int(att.get("size") or 0) > ATTACH_MAX_BYTES:
        return ""
    raw = client.call("GET", att["content"]).content
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader

            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
        if name.endswith(".docx"):
            import docx

            return "\n".join(p.text for p in docx.Document(io.BytesIO(raw)).paragraphs)
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"(attachment {att.get('filename')} could not be read: {type(exc).__name__})"


def issue_document(client: JiraClient, issue: dict, with_attachments: bool = False) -> Document:
    f = issue.get("fields") or {}
    key = issue["key"]
    project = (f.get("project") or {}).get("key") or "UNKNOWN"
    issue_type = (f.get("issuetype") or {}).get("name") or ""
    comment_block = f.get("comment") or {}
    comments = comment_block.get("comments") or []
    if int(comment_block.get("total") or 0) > len(comments):
        comments = client.all_comments(key)
    links = []
    for link in f.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        rel = (link.get("type") or {}).get("outward" if link.get("outwardIssue") else "inward", "")
        if other:
            links.append(f"{rel} {other.get('key')} {(other.get('fields') or {}).get('summary', '')}")
    parts = [
        f"# {key} {f.get('summary') or ''}",
        "",
        f"Project {project} {(f.get('project') or {}).get('name', '')}. Type {issue_type}. Status {_name(f.get('status'))}. "
        f"Priority {_name(f.get('priority'))}. Resolution {_name(f.get('resolution')) or 'unresolved'}.",
        f"Reporter {_name(f.get('reporter'))}. Assignee {_name(f.get('assignee')) or 'unassigned'}. Creator {_name(f.get('creator'))}.",
        f"Created {f.get('created')}. Updated {f.get('updated')}. Parent {(f.get('parent') or {}).get('key', '')}.",
        f"Labels {', '.join(f.get('labels') or []) or 'none'}. Components {', '.join(c.get('name', '') for c in f.get('components') or []) or 'none'}. "
        f"Fix versions {', '.join(v.get('name', '') for v in f.get('fixVersions') or []) or 'none'}.",
        "",
        "## Description",
        "",
        adf_text(f.get("description")).strip() or "(no description)",
    ]
    if links:
        parts += ["", "## Links", "", *links]
    if comments:
        parts += ["", "## Comments", ""]
        for c in comments:
            parts.append(f"{_name(c.get('author'))} on {c.get('created', '')[:10]}: {adf_text(c.get('body')).strip()}")
            parts.append("")
    if with_attachments:
        for att in f.get("attachment") or []:
            text = attachment_text(client, att).strip()
            if text:
                parts += ["", f"## Attachment {att.get('filename')}", "", text[:60000]]
    return Document(
        doc_id=f"jira:{key}",
        source_type="jira",
        title=f"{key} {f.get('summary') or ''}".strip(),
        kind="markdown",
        text="\n".join(parts),
        acl=[f"jira:project:{project}"],
        meta={"place": key, "source": f"{client.base}/browse/{key}", "kind_label": "jira_issue", "phase": "knowledge",
              "project": project, "issue_type": issue_type, "updated": f.get("updated", "")},
    )


def iter_issues(client: JiraClient, jql: str, with_attachments: bool = False, log=print) -> Iterator[Document]:
    for n, issue in enumerate(client.search(jql, FIELDS), start=1):
        if n % 200 == 0:
            log(f"[jira] fetched {n}")
        yield issue_document(client, issue, with_attachments)


def harvest(scope_jql: str = "created >= -7300d", full: bool = False, with_attachments: bool = False, log=print) -> dict:
    """First run (or full=True) pulls everything in scope; later runs pull only recent updates."""
    from .sync import sync_documents

    client = JiraClient()
    me = client.myself()
    log(f"[jira] login ok as {me.get('displayName')}")
    last = manifest.get_state("jira_last_harvest")
    started = datetime.now(timezone.utc)
    jql = f"({scope_jql})"
    if last and not full:
        since = datetime.fromisoformat(last) - timedelta(minutes=15)
        jql += f' AND updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
    jql += " ORDER BY updated ASC"
    log(f"[jira] JQL {jql}")
    stats = sync_documents(iter_issues(client, jql, with_attachments, log), "jira", delete_missing=False, log=log)
    manifest.set_state("jira_last_harvest", started.isoformat())
    stats["jql"] = jql
    stats["xray"] = harvest_xray(scope_jql, log) if os.environ.get("XRAY_CLIENT_ID") else "skipped: no XRAY_CLIENT_ID/XRAY_CLIENT_SECRET"
    return stats


def reconcile(scope_jql: str = "created >= -7300d", log=print) -> dict:
    """Delete indexed issues that no longer exist or are no longer visible."""
    from . import store

    client = JiraClient()
    visible = {f"jira:{i['key']}" for i in client.search(f"({scope_jql}) ORDER BY key", ["key"])}
    name = settings().collection
    removed = 0
    for doc_id in set(manifest.get_all(name, "jira")) - visible:
        store.delete_doc(doc_id, name)
        manifest.remove(name, doc_id)
        removed += 1
    log(f"[jira] reconcile removed {removed}")
    return {"visible": len(visible), "removed": removed}


def grants_from_jira(principal: str, email: str, token: str) -> list[str]:
    """Mirror project-level browse permission: the person's own token decides what they can retrieve."""
    from .acl import save_grants

    client = JiraClient(email=email, token=token)
    grants = [f"jira:project:{k}" for k in client.browsable_projects()]
    save_grants(principal, sorted(set(grants + ["liqa:internal"])))
    return grants


def harvest_xray(scope_jql: str, log=print) -> dict:
    from .sync import sync_documents

    resp = requests.post(
        "https://xray.cloud.getxray.app/api/v2/authenticate",
        json={"client_id": os.environ["XRAY_CLIENT_ID"], "client_secret": os.environ["XRAY_CLIENT_SECRET"]},
        timeout=60,
    )
    resp.raise_for_status()
    token = resp.json() if isinstance(resp.json(), str) else resp.text.strip('"')
    query = """query($jql: String, $start: Int) { getTests(jql: $jql, limit: 100, start: $start) {
      total results { jira(fields: ["key", "summary", "project"]) testType { name } steps { action data result } } } }"""

    def docs() -> Iterator[Document]:
        start = 0
        while True:
            r = requests.post(
                "https://xray.cloud.getxray.app/api/v2/graphql",
                json={"query": query, "variables": {"jql": f"({scope_jql}) AND issuetype = Test", "start": start}},
                headers={"Authorization": f"Bearer {token}"},
                timeout=120,
            )
            r.raise_for_status()
            page = r.json()["data"]["getTests"]
            for t in page["results"]:
                jf = t.get("jira") or {}
                key = jf.get("key")
                project = (jf.get("project") or {}).get("key", "UNKNOWN")
                rows = "\n".join(f"| {s.get('action') or ''} | {s.get('data') or ''} | {s.get('result') or ''} |" for s in t.get("steps") or [])
                yield Document(
                    doc_id=f"xray:{key}",
                    source_type="xray",
                    title=f"{key} {jf.get('summary', '')} test steps",
                    kind="markdown",
                    text=f"# {key} {jf.get('summary', '')}\n\nTest type {(t.get('testType') or {}).get('name', '')}.\n\n## Steps\n\n| Action | Data | Expected Result |\n|---|---|---|\n{rows}\n",
                    acl=[f"jira:project:{project}"],
                    meta={"place": key, "source": f"xray:{key}", "kind_label": "xray_test", "phase": "knowledge", "project": project, "issue_type": "Test"},
                )
            start += len(page["results"])
            if start >= page["total"] or not page["results"]:
                return

    return sync_documents(docs(), "xray", delete_missing=False, log=log)
