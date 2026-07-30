"""Helpers for locating and hashing the bundled executor agent."""

import hashlib
from pathlib import Path


def bundled_executor_agent_path() -> str | None:
    """Return the repo-bundled agent path when this checkout includes one."""

    path = Path(__file__).resolve().parents[3] / "agent" / "pki-executor.exe"
    return str(path) if path.is_file() else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
