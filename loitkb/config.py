"""Settings. Every value can be overridden by an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


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

    data_dir: Path = field(default_factory=lambda: Path(_env("LOITKB_DATA_DIR", r"C:\LIQA-memory")))

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

    liqa_home: Path = field(default_factory=lambda: Path(_env("LIQA_HOME", r"E:\LIQA")))
    agency_home: Path = field(default_factory=lambda: Path(_env("LIQA_AGENCY_HOME", r"E:\agency-agents")))
    extra_offsite_backup: list[Path] = field(default_factory=lambda: _paths("LOITKB_OFFSITE_DIRS", ""))

    snapshot_keep: int = int(_env("LOITKB_SNAPSHOT_KEEP", "7"))
    snapshot_max_age_hours: float = float(_env("LOITKB_SNAPSHOT_MAX_AGE_H", "26"))
    min_free_disk_gb: float = float(_env("LOITKB_MIN_FREE_GB", "5"))
    alert_webhook: str = field(default_factory=lambda: _env("LOITKB_ALERT_WEBHOOK", ""))

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


SKIP_PARTS = ("__pycache__", "node_modules", "secrets", ".env", "creds", "password", "captures", ".git")
MAX_FILE_BYTES = 1_500_000


def settings() -> Settings:
    return Settings()
