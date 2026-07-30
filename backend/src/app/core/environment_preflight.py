"""Control-plane readiness checks that must pass before ESXi cloning."""

from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.celery_app import celery_app
from app.core.agent_binary import sha256_file
from app.core.db.models import now_ms
from app.core.infrastructure import InfrastructureProfile, PkiRole
from app.core.jobs import transport
from app.core.settings import settings


class EnvironmentCheck(BaseModel):
    key: str
    ok: bool
    detail: str


class EnvironmentPreflight(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ready: bool
    checked_at: int = Field(alias="checkedAt")
    agent_sha256: str | None = Field(default=None, alias="agentSha256")
    checks: list[EnvironmentCheck]


def _agent_digest(path: Path) -> str:
    return sha256_file(path)


def _agent_binary_check(
    profiles: dict[PkiRole, InfrastructureProfile],
) -> tuple[EnvironmentCheck, str | None]:
    """Compare the deploy-time agent with every saved image qualification.

    Keep the failure detail actionable.  Previously an unset path, a missing
    file, no qualifications, and a real digest mismatch all collapsed into the
    same message, which left an operator with no way to tell what to repair.
    """

    configured_path = settings.executor_agent_path
    if not configured_path:
        return (
            EnvironmentCheck(
                key="agentBinary",
                ok=False,
                detail="EXECUTOR_AGENT_PATH is not configured on the API host.",
            ),
            None,
        )

    path = Path(configured_path)
    if not path.is_file():
        return (
            EnvironmentCheck(
                key="agentBinary",
                ok=False,
                detail="EXECUTOR_AGENT_PATH does not point to a readable file on the API host.",
            ),
            None,
        )

    try:
        digest = _agent_digest(path).lower()
    except OSError as exc:
        return (
            EnvironmentCheck(
                key="agentBinary",
                ok=False,
                detail=f"Could not hash the bundled executor agent: {exc}",
            ),
            None,
        )

    qualified = {
        role: profile.qualification.agent_sha256.lower()
        for role, profile in profiles.items()
        if profile.qualification is not None
    }
    if not qualified:
        return (
            EnvironmentCheck(
                key="agentBinary",
                ok=False,
                detail=(
                    f"Bundled agent SHA-256 is {digest}, but no image profile has "
                    "an agent qualification."
                ),
            ),
            digest,
        )

    mismatches = {
        role: expected for role, expected in qualified.items() if expected != digest
    }
    if mismatches:
        expected_by_role = ", ".join(
            f"{role}={expected}" for role, expected in sorted(mismatches.items())
        )
        return (
            EnvironmentCheck(
                key="agentBinary",
                ok=False,
                detail=(
                    f"Bundled agent SHA-256 is {digest}; mismatched qualified "
                    f"profile(s): {expected_by_role}. Requalify those image "
                    "revisions with this exact agent before deploying."
                ),
            ),
            digest,
        )

    return (
        EnvironmentCheck(
            key="agentBinary",
            ok=True,
            detail=(
                f"Bundled agent SHA-256 is {digest} and matches "
                f"{len(qualified)} qualified image profile(s)."
            ),
        ),
        digest,
    )


def preflight_control_plane(
    profiles: dict[PkiRole, InfrastructureProfile],
    *,
    mongo_ready: bool,
    check_worker: bool = True,
    require_agent: bool = True,
) -> EnvironmentPreflight:
    """Check persistence, broker, worker, callback, and bundled agent identity."""

    checks = [
        EnvironmentCheck(
            key="mongo",
            ok=mongo_ready,
            detail="MongoDB responded to ping."
            if mongo_ready
            else "MongoDB ping failed.",
        )
    ]
    try:
        valkey_ready = bool(transport._client.ping())  # noqa: SLF001 - shared client
    except Exception as exc:  # noqa: BLE001
        checks.append(
            EnvironmentCheck(
                key="valkey", ok=False, detail=f"Valkey ping failed: {exc}"
            )
        )
    else:
        checks.append(
            EnvironmentCheck(
                key="valkey", ok=valkey_ready, detail="Valkey responded to ping."
            )
        )

    # The agent phones home to the *effective* agent URL (AGENT_BACKEND_URL when
    # set, else BACKEND_PUBLIC_URL) — that's what's baked into executor.toml.
    callback = settings.effective_agent_backend_url or ""
    parsed = urlparse(callback)
    callback_ok = parsed.scheme in ("http", "https") and bool(parsed.netloc)
    overridden = bool(settings.agent_backend_url)
    checks.append(
        EnvironmentCheck(
            key="backendCallback",
            ok=callback_ok,
            detail=(
                f"Guest callback URL is '{callback}'"
                + (" (AGENT_BACKEND_URL override)." if overridden else ".")
                if callback_ok
                else "BACKEND_PUBLIC_URL (or AGENT_BACKEND_URL) must be an "
                "absolute HTTP(S) URL."
            ),
        )
    )

    if require_agent:
        agent_check, agent_digest = _agent_binary_check(profiles)
    else:
        agent_check = EnvironmentCheck(
            key="agentBinary",
            ok=True,
            detail="Windows executor agent is not required for this Linux product-only plan.",
        )
        agent_digest = None
    checks.append(agent_check)

    if check_worker:
        try:
            replies = celery_app.control.inspect(timeout=2.0).ping() or {}
        except Exception as exc:  # noqa: BLE001
            checks.append(
                EnvironmentCheck(
                    key="worker", ok=False, detail=f"Worker ping failed: {exc}"
                )
            )
        else:
            checks.append(
                EnvironmentCheck(
                    key="worker",
                    ok=bool(replies),
                    detail=(
                        f"{len(replies)} Celery worker(s) responded."
                        if replies
                        else "No Celery worker responded to ping."
                    ),
                )
            )
    return EnvironmentPreflight(
        ready=all(check.ok for check in checks),
        checkedAt=now_ms(),
        agentSha256=agent_digest,
        checks=checks,
    )
