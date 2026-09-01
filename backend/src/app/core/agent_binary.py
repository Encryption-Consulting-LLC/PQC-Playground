"""Helpers for locating and hashing the bundled executor agent.

The agent ships as one binary per guest platform — ``pki-executor.exe`` for the
Windows templates and a statically linked ``pki-executor`` for the Linux product
templates — so every lookup here is platform-keyed. They are *separate*
artifacts of the same crate release rather than one fat binary, which is why a
checkout can legitimately carry one and not the other: a console deploying only
Windows components needs no Linux agent, and vice versa.
"""

import hashlib
from pathlib import Path

#: Repo-bundled filename per guest platform (``core.firstboot.platform_for_
#: template``'s vocabulary), relative to ``backend/agent/``.
_BUNDLED_NAMES = {
    "windows": "pki-executor.exe",
    "linux": "pki-executor",
}


def bundled_executor_agent_path(platform: str = "windows") -> str | None:
    """Return the repo-bundled agent path for ``platform``, when present."""

    name = _BUNDLED_NAMES.get(platform)
    if name is None:
        return None
    path = Path(__file__).resolve().parents[3] / "agent" / name
    return str(path) if path.is_file() else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
