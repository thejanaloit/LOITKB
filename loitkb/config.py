"""Settings. Every value can be overridden by an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

# Bump when payload layout, chunking or vector config changes. A mismatch with the
# value stored in the manifest blocks writes until `python -m loitkb rebuild`.
SCHEMA_VERSION = "2"
DEFAULT_DATA_DIR = r"C:\LIQA-memory" if os.name == "nt" else str(Path.home() / ".liqa-memory")


def _load_local_env() -> None:
    """Machine-local secrets (Qdrant API key, principal) live in <data_dir>/loitkb.env,
    outside every repo. Real environment variables always win."""
    path = Path(os.environ.get("LOITKB_DATA_DIR", DEFAULT_DATA_DIR)) / "loitkb.env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_local_env()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _sibling(env_name: str, sibling: str, legacy: str) -> Path:
    """Env var, else a checkout next to this repo, else the original machine's path."""
    if os.environ.get(env_name):
        return Path(os.environ[env_name])
    candidate = REPO_ROOT.parent / sibling
    return candidate if candidate.exists() else Path(legacy)


def _paths(name: str, default: str) -> list[Path]:
    return [Path(p) for p in _env(name, default).split(os.pathsep) if p.strip()]


# Agency rules that describe how QA, delivery and review are done. Marketing,
# game, and social-media personas are deliberately excluded: they are noise for
# LOLC QA retrieval.
QA_AGENCY_RULES = [
    "lolc-qa-environment",
    "liqa-perfect-100",
    "ipay-vkyc-product",
    "evidence-collector",
    "reality-checker",
    "test-results-analyzer",
    "test-automation-engineer",
    "api-tester",
    "accessibility-auditor",
    "performance-benchmarker",
    "jira-workflow-steward",
    "code-reviewer",
    "workflow-architect",
    "senior-project-manager",
    "agents-orchestrator",
    "technical-writer",
    "application-security-engineer",
    "compliance-auditor",
    "minimal-change-engineer",
    "git-workflow-master",
]


@dataclass
class Settings:
    qdrant_url: str = field(default_factory=lambda: _env("LOITKB_QDRANT_URL", "http://127.0.0.1:6333"))
    qdrant_api_key: str = field(default_factory=lambda: _env("LOITKB_QDRANT_API_KEY", ""))
    collection: str = field(default_factory=lambda: _env("LOITKB_COLLECTION", "loitkb"))
    scratch_collection: str = field(default_factory=lambda: _env("LOITKB_SCRATCH_COLLECTION", "loitkb_eval_scratch"))

    data_dir: Path = field(default_factory=lambda: Path(_env("LOITKB_DATA_DIR", DEFAULT_DATA_DIR)))

    dense_model: str = field(default_factory=lambda: _env("LOITKB_DENSE_MODEL", "BAAI/bge-small-en-v1.5"))
    dense_size: int = 384
    sparse_model: str = field(default_factory=lambda: _env("LOITKB_SPARSE_MODEL", "Qdrant/bm25"))
    rerank_model: str = field(default_factory=lambda: _env("LOITKB_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2"))

    chunk_max_chars: int = int(_env("LOITKB_CHUNK_MAX", "900"))
    chunk_min_chars: int = int(_env("LOITKB_CHUNK_MIN", "60"))
    chunk_overlap_chars: int = int(_env("LOITKB_CHUNK_OVERLAP", "150"))

    candidates: int = int(_env("LOITKB_CANDIDATES", "20"))
    rerank_enabled: bool = _env("LOITKB_RERANK", "1") == "1"
    rerank_threads: int = int(_env("LOITKB_RERANK_THREADS", "8"))
    # Cross-encoder cost grows with input length; the head of a chunk carries the signal.
    rerank_max_chars: int = int(_env("LOITKB_RERANK_MAX_CHARS", "600"))
    # Final order = weight * RRF(rerank rank) + (1 - weight) * RRF(hybrid rank). Pure
    # cross-encoder order lost to hybrid on the golden set; the blend beats both.
    rerank_weight: float = float(_env("LOITKB_RERANK_WEIGHT", "0.7"))
    # Only the head of the hybrid list goes through the cross-encoder; the tail keeps
    # hybrid order. Bounds latency on a busy shared machine.
    rerank_top: int = int(_env("LOITKB_RERANK_TOP", "8"))
    # Past this budget the query returns hybrid order, flagged degraded, instead of waiting.
    rerank_budget_ms: int = int(_env("LOITKB_RERANK_BUDGET_MS", "1800"))
    # Below this reranker score the system abstains instead of answering.
    abstain_rerank_score: float = float(_env("LOITKB_ABSTAIN_RERANK", "-4.0"))
    abstain_fused_score: float = float(_env("LOITKB_ABSTAIN_FUSED", "0.0"))

    liqa_home: Path = field(default_factory=lambda: _sibling("LIQA_HOME", "LIQA", r"E:\LIQA"))
    agency_home: Path = field(default_factory=lambda: _sibling("LIQA_AGENCY_HOME", "agency-agents", r"E:\agency-agents"))
    extra_offsite_backup: list[Path] = field(default_factory=lambda: _paths("LOITKB_OFFSITE_DIRS", ""))

    snapshot_keep: int = int(_env("LOITKB_SNAPSHOT_KEEP", "7"))
    snapshot_max_age_hours: float = float(_env("LOITKB_SNAPSHOT_MAX_AGE_H", "26"))
    min_free_disk_gb: float = float(_env("LOITKB_MIN_FREE_GB", "5"))
    # Writes (sync, remember, backup) refuse to run below this; health only warns at min_free_disk_gb.
    hard_min_free_disk_gb: float = field(default_factory=lambda: float(_env("LOITKB_HARD_MIN_FREE_GB", "1")))
    # A sync that would delete more than this share of a source's documents is treated
    # as a broken source (wrong path, unmounted drive) and deletes nothing.
    max_delete_fraction: float = field(default_factory=lambda: float(_env("LOITKB_MAX_DELETE_FRACTION", "0.5")))
    max_delete_floor: int = field(default_factory=lambda: int(_env("LOITKB_MAX_DELETE_FLOOR", "20")))
    alert_webhook: str = field(default_factory=lambda: _env("LOITKB_ALERT_WEBHOOK", ""))
    # Consecutive WARN runs before a WARN is alerted (FAIL alerts immediately).
    warn_alert_after: int = field(default_factory=lambda: int(_env("LOITKB_WARN_ALERT_AFTER", "3")))
    query_log_max_mb: float = field(default_factory=lambda: float(_env("LOITKB_QUERY_LOG_MAX_MB", "20")))
    require_api_key: bool = field(default_factory=lambda: _env("LOITKB_REQUIRE_API_KEY", "0") == "1")

    llm_base_url: str = field(default_factory=lambda: _env("LOITKB_LLM_BASE_URL", ""))
    llm_model: str = field(default_factory=lambda: _env("LOITKB_LLM_MODEL", ""))
    llm_api_key: str = field(default_factory=lambda: _env("LOITKB_LLM_API_KEY", ""))

    @property
    def model_cache(self) -> Path:
        return self.data_dir / "models"

    @property
    def manifest_db(self) -> Path:
        return self.data_dir / "manifest.sqlite"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def memory_journal(self) -> Path:
        return self.data_dir / "memories.jsonl"

    @property
    def query_log(self) -> Path:
        return self.data_dir / "logs" / "queries.jsonl"

    @property
    def health_file(self) -> Path:
        return self.data_dir / "health.json"

    @property
    def health_history(self) -> Path:
        return self.data_dir / "logs" / "health-history.jsonl"

    @property
    def access_policy(self) -> Path:
        return Path(_env("LOITKB_ACCESS_POLICY", str(self.data_dir / "access-policy.json")))

    @property
    def jira_env(self) -> Path:
        return self.data_dir / "jira.env"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def alerts_file(self) -> Path:
        return self.data_dir / "logs" / "alerts.jsonl"

    @property
    def schema_signature(self) -> str:
        return f"{SCHEMA_VERSION}|{self.dense_model}|{self.dense_size}|{self.sparse_model}"

    def workspace_sources(self) -> list[tuple[Path, str]]:
        """(folder, phase) for LIQA run artifacts: harvest, stories, maps, test cases, bugs."""
        folders = {
            "assignedTasks": "planning",
            "knowledgeBase": "analysis",
            "UserStories": "analysis",
            "ExistingTestCases": "analysis",
            "map": "analysis",
            "NewTestCases": "design",
            "bugs": "execution",
        }
        root = self.liqa_home / "workspace"
        if not root.exists():
            return []
        return [(run / name, phase) for run in sorted(root.iterdir()) if run.is_dir() for name, phase in folders.items() if (run / name).is_dir()]

    @property
    def golden_set(self) -> Path:
        return Path(_env("LOITKB_GOLDEN", str(REPO_ROOT / "eval" / "golden.jsonl")))

    def file_sources(self) -> list[tuple[Path, list[str], str]]:
        """(root, glob patterns, acl tag) for every file source."""
        liqa = self.liqa_home
        rules = self.agency_home / ".cursor" / "rules"
        return [
            (liqa / "docs", ["**/*.md"], "liqa:internal"),
            (liqa / "skills", ["**/*.md"], "liqa:internal"),
            (liqa / "packaging", ["*.md", "lolc/**/*.md", "lolc/**/*.yaml", "lolc/**/*.yml", "industry/*.md"], "liqa:internal"),
            (liqa / "packaging" / "industry" / "agency-qa-trained", [f"{name}.md" for name in QA_AGENCY_RULES], "liqa:internal"),
            (liqa / "company", ["**/*.md"], "liqa:internal"),
            (liqa / "mcp", ["laws.py", "flow.py"], "liqa:internal"),
            (rules, [f"{name}.mdc" for name in QA_AGENCY_RULES], "liqa:internal"),
        ]


SKIP_PARTS = ("__pycache__", "node_modules", "secrets", ".env", "creds", "password", "captures", ".git", "pw-edge-profile", ".chrome-")
MAX_FILE_BYTES = 1_500_000


def settings() -> Settings:
    return Settings()
