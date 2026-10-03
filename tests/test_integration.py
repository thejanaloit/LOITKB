"""End-to-end against the live Qdrant in a throwaway collection. Skipped when Qdrant is down."""
import pytest

from loitkb import manifest, store
from loitkb.sources import Document

COLL = "loitkb_pytest"


def _qdrant_up() -> bool:
    try:
        store.server_version()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _qdrant_up(), reason="Qdrant not reachable")


def _doc(doc_id, text, acl=("t:a",)):
    return Document(doc_id=doc_id, source_type="pytest", title=doc_id, kind="markdown", text=text, acl=list(acl))


@pytest.fixture(autouse=True)
def clean():
    yield
    if store.client().collection_exists(COLL):
        store.client().delete_collection(COLL)
    manifest.clear(COLL)


def test_sync_replace_delete_and_acl():
    from loitkb.retrieve import retrieve
    from loitkb.sync import sync_documents

    quiet = lambda *_: None
    a = _doc("p:a", "# A\n\nThe recovery stage column appears on the account inquiry modal for Kenya.")
    b = _doc("p:b", "# B\n\nThis document lives under a different permission and must stay hidden.", acl=("t:b",))
    s1 = sync_documents([a, b], "pytest", COLL, log=quiet)
    assert s1["indexed"] == 2

    s2 = sync_documents([a], "pytest", COLL, log=quiet)
    assert s2 == {**s2, "skipped": 1, "deleted": 1}
    assert manifest.total_chunks(COLL) == store.count(COLL)

    r = retrieve("recovery stage column", "x", grants=["t:a"], collection=COLL, log=False)
    assert r["hits"] and r["hits"][0]["doc_id"] == "p:a"
    assert retrieve("recovery stage column", "x", grants=["t:zzz"], collection=COLL, log=False)["hits"] == []


def test_point_ids_are_stable():
    from loitkb.sync import index_document

    d = _doc("p:stable", "# S\n\nStable identifiers mean re-indexing never duplicates a chunk in the index.")
    index_document(d, COLL)
    first = sorted(str(p.id) for p in store.client().scroll(COLL, limit=50)[0])
    index_document(d, COLL, force=True)
    second = sorted(str(p.id) for p in store.client().scroll(COLL, limit=50)[0])
    assert first == second
