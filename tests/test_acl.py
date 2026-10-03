import pytest

from loitkb.acl import AccessDenied, expand_tag, expand_tags, grants_for, save_grants, load_policy


def test_expand_tag_adds_wildcard_parents():
    assert expand_tag("jira:project:PF") == ["jira:*", "jira:project:*", "jira:project:PF"]
    assert expand_tag("liqa:internal") == ["liqa:*", "liqa:internal"]


def test_expand_tags_dedupes():
    assert expand_tags(["jira:project:PF", "jira:project:VV"]).count("jira:*") == 1


def test_anonymous_and_unknown_principals_denied():
    policy = {"principals": {"a": ["x:y"]}, "groups": {}, "members": {}}
    with pytest.raises(AccessDenied):
        grants_for("", policy)
    with pytest.raises(AccessDenied):
        grants_for("stranger", policy)


def test_group_membership_merges_grants():
    policy = {"principals": {"m": ["jira:project:PF"]}, "groups": {"qa": ["liqa:internal"]}, "members": {"qa": ["m"]}}
    assert grants_for("m", policy) == ["jira:project:PF", "liqa:internal"]


def test_save_grants_roundtrip(tmp_path):
    path = tmp_path / "policy.json"
    save_grants("p", ["b", "a", "a"], path)
    assert load_policy(path)["principals"]["p"] == ["a", "b"]
