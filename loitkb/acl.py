"""Permission filtering. Deny by default.

Every chunk stores ACL tags. A tag `jira:project:PF` is stored with its wildcard
parents (`jira:*`, `jira:project:*`) so a policy grant of `jira:*` or an exact
project grant both match with a single keyword MatchAny filter, applied inside
Qdrant before scoring. A caller with no grants sees nothing.

Policy file (gitignored, under the data dir):

    {"principals": {"thejana": ["liqa:internal", "jira:project:PF"]},
     "groups": {"qa": ["liqa:internal"]},
     "members": {"qa": ["methmi"]}}
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import settings


class AccessDenied(PermissionError):
    pass


def expand_tag(tag: str) -> list[str]:
    parts = tag.split(":")
    tags = [":".join(parts[:i]) + ":*" for i in range(1, len(parts))]
    tags.append(tag)
    return tags


def expand_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        for t in expand_tag(tag):
            if t not in out:
                out.append(t)
    return out


def load_policy(path: Path | None = None) -> dict:
    path = path or settings().access_policy
    if not path.exists():
        return {"principals": {}, "groups": {}, "members": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("principals", {})
    data.setdefault("groups", {})
    data.setdefault("members", {})
    return data


def grants_for(principal: str, policy: dict | None = None) -> list[str]:
    if not principal:
        raise AccessDenied("A principal is required. Anonymous queries are denied.")
    policy = policy if policy is not None else load_policy()
    grants = list(policy["principals"].get(principal, []))
    for group, members in policy["members"].items():
        if principal in members:
            grants.extend(policy["groups"].get(group, []))
    grants = sorted(set(grants))
    if not grants:
        raise AccessDenied(f"Principal '{principal}' has no grants in the access policy.")
    return grants


def qdrant_filter(grants: list[str]):
    from qdrant_client.models import FieldCondition, MatchAny

    return FieldCondition(key="acl", match=MatchAny(any=grants))


def save_grants(principal: str, grants: list[str], path: Path | None = None) -> dict:
    path = path or settings().access_policy
    policy = load_policy(path)
    policy["principals"][principal] = sorted(set(grants))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2), encoding="utf-8")
    return policy
