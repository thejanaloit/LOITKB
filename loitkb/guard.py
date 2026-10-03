"""Write-path guards: free disk, one writer at a time, and index schema/model version."""
from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .config import settings


class GuardError(RuntimeError):
    pass


def free_gb(path: Path | None = None) -> float:
    path = path or settings().data_dir
    probe = path if path.exists() else Path(path.anchor or "/")
    return shutil.disk_usage(probe).free / 1e9


def require_disk(action: str) -> None:
    cfg = settings()
    free = free_gb()
    if free < cfg.hard_min_free_disk_gb:
        raise GuardError(f"Refusing to {action}: {free:.1f} GB free on the data drive, need {cfg.hard_min_free_disk_gb} GB (LOITKB_HARD_MIN_FREE_GB).")


@contextmanager
def single_writer(action: str, collection: str | None = None, wait_s: float = 600):
    """Per-collection OS file lock, released automatically if the process dies, so it never goes stale."""
    name = collection or settings().collection
    path = settings().data_dir / "locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    deadline = time.time() + wait_s
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                fh.close()
                raise GuardError(f"Refusing to {action}: another LOITKB writer holds {path} for over {wait_s:.0f}s.")
            time.sleep(1)
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def check_schema(collection: str) -> str:
    """Adopt the signature on a fresh or legacy index; block writes when it changed."""
    from . import manifest

    cfg = settings()
    key = f"schema:{collection}"
    stored = manifest.get_state(key)
    if not stored:
        manifest.set_state(key, cfg.schema_signature)
        return "adopted"
    if stored != cfg.schema_signature:
        raise GuardError(
            f"Index '{collection}' was built with '{stored}', config is now '{cfg.schema_signature}'. "
            "Mixed embeddings give wrong answers. Run: python -m loitkb rebuild"
        )
    return "ok"
