"""Guards that keep the index safe on a new or misconfigured machine."""
import pytest

from loitkb import backup, health, manifest, store
from loitkb.sources import Document, acl_for_task, redact, workspace_document


def test_redact_strips_tokens_and_passwords():
    text = "token ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz123 and password: hunter2 and Bearer abcdefghijklmnopqrstuvwxyz0123"
    out = redact(text)
    assert "ATATT3x" not in out and "hunter2" not in out and "abcdefghijklmnopqrstuvwxyz0123" not in out
    assert "[REDACTED" in out


def test_acl_follows_task_key():
    assert acl_for_task("PF-59486") == ["jira:project:PF"]
    assert acl_for_task("ssp-38278") == ["jira:project:SSP"]
    assert acl_for_task("") == ["liqa:internal"]
    assert acl_for_task("not a key") == ["liqa:internal"]


def test_workspace_document_derives_key_phase_and_skips_secrets(tmp_path):
    story = tmp_path / "UserStories" / "PF-12345" / "story.md"
    story.parent.mkdir(parents=True)
    story.write_text("# Recovery status\n\nThe modal shows the recovery stage.", encoding="utf-8")
    doc = workspace_document(story, "analysis")
    assert doc and doc.acl == ["jira:project:PF"] and doc.meta["task_key"] == "PF-12345"
    assert doc.meta["kind_label"] == "UserStories" and doc.source_type == "workspace"

    secret = tmp_path / "secrets" / "PF-1" / "creds.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("password: x", encoding="utf-8")
    assert workspace_document(secret) is None
    assert workspace_document(tmp_path / "missing.md") is None


@pytest.mark.parametrize(
    "previous,n,expected",
    [
        (["OK", "WARN", "WARN"], 3, True),
        (["WARN", "WARN", "WARN"], 3, False),  # streak already alerted
        (["WARN", "OK", "WARN"], 3, False),
        (["WARN", "WARN"], 3, True),
        ([], 1, True),
        (["WARN"], 1, False),
    ],
)
def test_warn_streak_fires_once(previous, n, expected):
    assert health.warn_streak_starts(previous, n) is expected


def test_restore_over_live_requires_confirm(monkeypatch):
    meta = {"path": "x", "snapshot": "s", "collection": __import__("loitkb.config").config.settings().collection, "points": 1}
    monkeypatch.setattr(backup, "verify", lambda m: True)
    with pytest.raises(RuntimeError, match="--yes"):
        backup.restore(meta)


def test_send_alert_writes_local_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOITKB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOITKB_ALERT_WEBHOOK", "")
    out = health.send_alert("OK", "pytest alert channel check")
    assert out["webhook"] == "not configured"
    assert "pytest alert channel check" in (tmp_path / "logs" / "alerts.jsonl").read_text(encoding="utf-8")


def _qdrant_up() -> bool:
    try:
        store.server_version()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _qdrant_up(), reason="Qdrant not reachable")
def test_sync_refuses_mass_deletion(monkeypatch):
    from loitkb.sync import sync_documents

    coll = "loitkb_pytest_guard"
    monkeypatch.setenv("LOITKB_MAX_DELETE_FLOOR", "2")
    quiet = lambda *_: None
    docs = [Document(f"g:{i}", "pytest", f"g{i}", "markdown", f"# G{i}\n\nGuard document number {i} with enough text to chunk.", ["t:a"]) for i in range(6)]
    try:
        assert sync_documents(docs, "pytest", coll, log=quiet)["indexed"] == 6
        res = sync_documents([], "pytest", coll, log=quiet)  # a source that "vanished"
        assert res["deleted"] == 0 and "deletion_blocked" in res
        assert manifest.total_chunks(coll) == store.count(coll) > 0
        res = sync_documents(docs[:5], "pytest", coll, log=quiet)  # one real removal is fine
        assert res["deleted"] == 1
    finally:
        if store.client().collection_exists(coll):
            store.client().delete_collection(coll)
        manifest.clear(coll)
        manifest.set_state(f"schema:{coll}", "")
