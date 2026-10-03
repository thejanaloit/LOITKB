from loitkb import manifest


def test_put_get_remove_and_filters(tmp_path):
    db = tmp_path / "m.sqlite"
    manifest.put("c", "d1", "file", "h1", 3, "t", db)
    manifest.put("c", "d2", "jira", "h2", 2, "t", db)
    manifest.put("other", "d3", "file", "h3", 9, "t", db)
    assert set(manifest.get_all("c", path=db)) == {"d1", "d2"}
    assert set(manifest.get_all("c", "file", db)) == {"d1"}
    assert manifest.total_chunks("c", db) == 5
    manifest.remove("c", "d1", db)
    assert set(manifest.get_all("c", path=db)) == {"d2"}
    manifest.clear("c", db)
    assert manifest.get_all("c", path=db) == {}
    assert manifest.total_chunks("other", db) == 9


def test_state(tmp_path):
    db = tmp_path / "m.sqlite"
    assert manifest.get_state("k", "none", db) == "none"
    manifest.set_state("k", "v", db)
    assert manifest.get_state("k", path=db) == "v"
