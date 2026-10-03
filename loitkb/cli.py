"""python -m loitkb <command>"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timedelta, timezone


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="loitkb", description="LOITKB local hybrid RAG knowledge base")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="incremental index of all sources")
    s.add_argument("--source", action="append", choices=["file", "flow", "memory"])
    s.add_argument("--force", action="store_true")
    sub.add_parser("rebuild", help="drop and re-index everything")

    for name in ("search", "ask"):
        q = sub.add_parser(name)
        q.add_argument("query")
        q.add_argument("--as", dest="principal", default=getpass.getuser().lower())
        q.add_argument("--limit", type=int, default=5)
        if name == "search":
            q.add_argument("--mode", choices=["hybrid", "dense", "sparse"], default="hybrid")
            q.add_argument("--no-rerank", action="store_true")

    r = sub.add_parser("remember")
    r.add_argument("text")
    r.add_argument("--task", default="")
    r.add_argument("--phase", default="completion")

    g = sub.add_parser("grant", help="set a principal's ACL grants")
    g.add_argument("principal")
    g.add_argument("grants", nargs="+")

    sub.add_parser("health")
    sub.add_parser("backup")
    sub.add_parser("backups")
    rs = sub.add_parser("restore")
    rs.add_argument("--into", default=None, help="restore into another collection name (test restore)")

    e = sub.add_parser("eval")
    e.add_argument("--skip-quality", action="store_true")
    sub.add_parser("monitor", help="health; backup when due; daily eval (for the scheduled task)")

    j = sub.add_parser("jira", help="harvest Jira (needs JIRA_API_TOKEN)")
    j.add_argument("--scope", default="created >= -7300d")
    j.add_argument("--full", action="store_true")
    j.add_argument("--attachments", action="store_true")
    jr = sub.add_parser("jira-reconcile")
    jr.add_argument("--scope", default="created >= -7300d")
    jg = sub.add_parser("jira-grants", help="derive a person's grants from their own Jira token")
    jg.add_argument("principal")
    jg.add_argument("email")

    a = p.parse_args(argv)

    if a.cmd == "sync":
        from .sync import sync

        _print(sync(a.source, force=a.force))
    elif a.cmd == "rebuild":
        from .sync import rebuild

        _print(rebuild())
    elif a.cmd == "search":
        from .retrieve import retrieve

        res = retrieve(a.query, a.principal, limit=a.limit, mode=a.mode, rerank=not a.no_rerank)
        for h in res["hits"]:
            h["text"] = h["text"][:300]
        _print(res)
    elif a.cmd == "ask":
        from .answer import ask

        _print(ask(a.query, a.principal, limit=a.limit))
    elif a.cmd == "remember":
        from .sources import journal_memory
        from .sync import index_document

        doc = journal_memory(a.text, "memory", a.phase, a.task, "cli")
        _print({"doc_id": doc.doc_id, "result": index_document(doc)})
    elif a.cmd == "grant":
        from .acl import save_grants

        _print(save_grants(a.principal, a.grants)["principals"][a.principal])
    elif a.cmd == "health":
        from .health import run

        rep = run()
        _print(rep)
        return 0 if rep["overall"] != "FAIL" else 2
    elif a.cmd == "backup":
        from .backup import create

        _print(create())
    elif a.cmd == "backups":
        from .backup import list_backups

        _print(list_backups())
    elif a.cmd == "restore":
        from .backup import restore

        _print(restore(target_collection=a.into))
    elif a.cmd == "eval":
        from .config import settings
        from .evaluator import run

        rep = run(skip_quality=a.skip_quality)
        print((settings().reports_dir / "latest.md").read_text(encoding="utf-8"))
        return 0 if rep["verdict"] == "INDUSTRY-GRADE PASS" else 3
    elif a.cmd == "monitor":
        return monitor()
    elif a.cmd == "jira":
        from .jira import harvest

        _print(harvest(a.scope, full=a.full, with_attachments=a.attachments))
    elif a.cmd == "jira-reconcile":
        from .jira import reconcile

        _print(reconcile(a.scope))
    elif a.cmd == "jira-grants":
        from .jira import grants_from_jira

        token = getpass.getpass("That person's Jira API token (not stored): ")
        _print(grants_from_jira(a.principal, a.email, token))
    return 0


def monitor() -> int:
    """Hourly: sync changed files, health, backup if older than 24 h, evaluator once a day."""
    from . import backup, health, manifest
    from .sync import sync

    log_lines = []
    try:
        sync(log=log_lines.append)
    except Exception as exc:
        log_lines.append(f"sync failed: {exc}")
    rep = health.run()
    latest = backup.latest()
    due = not latest or datetime.now(timezone.utc) - datetime.strptime(latest["created_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) > timedelta(hours=24)
    if due and rep["overall"] != "FAIL":
        backup.create()
        rep = health.run()
    last_eval = manifest.get_state("last_eval")
    if rep["overall"] != "FAIL" and (not last_eval or datetime.now(timezone.utc) - datetime.fromisoformat(last_eval) > timedelta(hours=24)):
        from .evaluator import run

        run()
    print(json.dumps({"health": rep["overall"], "backup_taken": due, "sync": log_lines[-3:]}, default=str))
    return 0 if rep["overall"] != "FAIL" else 2


if __name__ == "__main__":
    sys.exit(main())
