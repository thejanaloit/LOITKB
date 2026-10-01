"""Pull every Jira issue this login can see and store it in Qdrant.

Reads gitignored credentials only. Never prints the token.
Set C:\\LIQA-memory\\jira.env or the environment:

    JIRA_BASE_URL=https://lolcgroupdev.atlassian.net
    JIRA_EMAIL=you@company.com
    JIRA_API_TOKEN=the-api-token

Jira Cloud rejects the account password. Use an API token from
https://id.atlassian.com/manage-profile/security/api-tokens
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_rag import _windows, upsert  # noqa: E402

ENV_FILE = Path(os.environ.get("LIQA_MEMORY_DIR", r"C:\LIQA-memory")) / "jira.env"
CHECKPOINT = Path(os.environ.get("LIQA_MEMORY_DIR", r"C:\LIQA-memory")) / "jira-checkpoint.json"
JQL = 'created >= -7300d ORDER BY created ASC'
FIELDS = [
    "summary",
    "description",
    "comment",
    "issuetype",
    "status",
    "project",
    "assignee",
    "reporter",
    "creator",
    "created",
    "updated",
    "labels",
    "parent",
]


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _cfg() -> tuple[str, str, str]:
    _load_env()
    base = (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL") or ""
    token = os.environ.get("JIRA_API_TOKEN") or ""
    if not (base and email and token):
        raise SystemExit(
            "Missing JIRA_BASE_URL, JIRA_EMAIL, or JIRA_API_TOKEN. "
            f"Put them in {ENV_FILE}. Do not commit that file."
        )
    return base, email, token


def _request(base: str, email: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Basic " + auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise SystemExit(f"Jira HTTP {exc.code} on {path}: {detail}") from exc


def _text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(part for part in (_text(item) for item in node) if part)
    if isinstance(node, dict):
        bits = []
        if node.get("type") == "text" and node.get("text"):
            bits.append(str(node["text"]))
        bits.append(_text(node.get("content")))
        return " ".join(part for part in bits if part).strip()
    return ""


def _name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("name") or value.get("key") or "")
    return ""


def _issue_text(issue: dict) -> str:
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    issue_type = fields.get("issuetype") or {}
    comments = ((fields.get("comment") or {}).get("comments") or [])
    comment_text = []
    for comment in comments:
        body = _text(comment.get("body"))
        if body:
            comment_text.append(f"{_name(comment.get('author'))}: {body}")
    lines = [
        f"{issue.get('key')} {fields.get('summary') or ''}",
        f"Project {project.get('key')} {project.get('name')}",
        f"Type {issue_type.get('name')} Status {_name(fields.get('status'))}",
        f"Reporter {_name(fields.get('reporter'))} Assignee {_name(fields.get('assignee'))} Creator {_name(fields.get('creator'))}",
        f"Created {fields.get('created')} Updated {fields.get('updated')}",
        f"Labels {', '.join(fields.get('labels') or [])}",
        _text(fields.get("description")),
        "Comments",
        "\n".join(comment_text),
    ]
    return "\n".join(line for line in lines if line and line.strip())


def _checkpoint() -> dict:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    return {"nextPageToken": None, "issues": 0, "users": 0, "done": False}


def _save(state: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(state, indent=2), encoding="utf-8")


def harvest_users(base: str, email: str, token: str, state: dict) -> None:
    start = int(state.get("userStart") or 0)
    while True:
        page = _request(base, email, token, "GET", f"/rest/api/3/users/search?startAt={start}&maxResults=100")
        if not isinstance(page, list) or not page:
            break
        records = []
        for user in page:
            account = user.get("accountId") or user.get("displayName")
            text = f"Jira user {user.get('displayName')} account {account} active {user.get('active')}"
            records.append(
                {
                    "key": f"jira-user:{account}",
                    "text": text,
                    "payload": {"kind": "jira_user", "phase": "knowledge", "place": account, "source": "jira"},
                }
            )
        if records:
            upsert(records)
        start += len(page)
        state["userStart"] = start
        state["users"] = start
        _save(state)
        print(f"users={start}", flush=True)
        if len(page) < 100:
            break


def harvest_issues(base: str, email: str, token: str, state: dict) -> None:
    token_page = state.get("nextPageToken")
    while True:
        body = {"jql": JQL, "maxResults": 50, "fields": FIELDS}
        if token_page:
            body["nextPageToken"] = token_page
        page = _request(base, email, token, "POST", "/rest/api/3/search/jql", body)
        issues = page.get("issues") or []
        records = []
        for issue in issues:
            fields = issue.get("fields") or {}
            issue_type = (fields.get("issuetype") or {}).get("name") or ""
            project = (fields.get("project") or {}).get("key") or ""
            text = _issue_text(issue)
            pieces = _windows(text) or ([text.strip()] if len(text.strip()) >= 8 else [])
            for index, piece in enumerate(pieces):
                records.append(
                    {
                        "key": f"jira:{issue.get('key')}:{fields.get('updated')}:{index}",
                        "text": piece,
                        "payload": {
                            "kind": "jira_issue",
                            "phase": "knowledge",
                            "place": issue.get("key"),
                            "source": f"{project}:{issue_type}",
                            "project": project,
                            "issue_type": issue_type,
                        },
                    }
                )
        if records:
            upsert(records)
        state["issues"] = int(state.get("issues") or 0) + len(issues)
        token_page = page.get("nextPageToken")
        state["nextPageToken"] = token_page
        _save(state)
        print(f"issues={state['issues']}", flush=True)
        if page.get("isLast") or not token_page:
            state["done"] = True
            _save(state)
            break


def main() -> None:
    base, email, token = _cfg()
    me = _request(base, email, token, "GET", "/rest/api/3/myself")
    print(f"login_ok accountType={me.get('accountType')}", flush=True)
    state = _checkpoint()
    if not state.get("users_done"):
        harvest_users(base, email, token, state)
        state["users_done"] = True
        _save(state)
    if not state.get("done"):
        harvest_issues(base, email, token, state)
    print(f"finished issues={state.get('issues')} users={state.get('users')}", flush=True)


if __name__ == "__main__":
    main()
