"""Control-plane readiness preflight."""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import environment_preflight as subject  # noqa: E402
from app.core.infrastructure import ImageQualification, infrastructure_profiles_from_doc  # noqa: E402


def test_control_plane_requires_callback_agent_broker_and_worker(monkeypatch, tmp_path):
    agent = tmp_path / "agent.exe"
    agent.write_bytes(b"qualified agent")
    digest = subject._agent_digest(agent)
    profiles = infrastructure_profiles_from_doc({})
    for profile in profiles.values():
        profile.qualification = ImageQualification(
            baseChangeVersion="1",
            windowsBuild=26100,
            runnerVersion="2",
            agentSha256=digest,
            validatedAt=1,
            mlDsa87Available=True,
            systemContextValidated=True,
            timeSynchronized=True,
            windowsUpdatesCurrent=True,
            backendCallbackReachable=True,
            agentCommands=[
                "ca.publish_crl",
                "ca.uninstall",
                "dc.remove_forest",
                "dns.remove_resources",
                "dns.verify_absent",
                "domain.leave",
                "iis.remove_certenroll",
                "ocsp.remove",
            ],
            publicationManifestVersion=1,
        )
    monkeypatch.setattr(subject.settings, "executor_agent_path", str(agent))
    monkeypatch.setattr(subject.settings, "backend_public_url", "https://pki.example")
    monkeypatch.setattr(subject.transport._client, "ping", lambda: True)
    monkeypatch.setattr(
        subject.celery_app.control,
        "inspect",
        lambda timeout: type(
            "Inspect", (), {"ping": lambda self: {"worker": {"ok": "pong"}}}
        )(),
    )

    result = subject.preflight_control_plane(profiles, mongo_ready=True)

    assert result.ready is True
    assert result.agent_sha256 == digest
    assert {check.key for check in result.checks} == {
        "mongo",
        "valkey",
        "backendCallback",
        "agentBinary",
        "worker",
    }


def test_control_plane_reports_every_missing_prerequisite(monkeypatch):
    profiles = infrastructure_profiles_from_doc({})
    monkeypatch.setattr(subject.settings, "executor_agent_path", None)
    monkeypatch.setattr(subject.settings, "backend_public_url", None)
    monkeypatch.setattr(subject.transport._client, "ping", lambda: False)
    monkeypatch.setattr(
        subject.celery_app.control,
        "inspect",
        lambda timeout: type("Inspect", (), {"ping": lambda self: {}})(),
    )

    result = subject.preflight_control_plane(profiles, mongo_ready=False)

    assert result.ready is False
    assert result.agent_sha256 is None
    assert all(not check.ok for check in result.checks)
    agent = next(check for check in result.checks if check.key == "agentBinary")
    assert agent.detail == "EXECUTOR_AGENT_PATH is not configured on the API host."


def test_control_plane_reports_actual_digest_and_mismatched_roles(
    monkeypatch, tmp_path
):
    agent = tmp_path / "agent.exe"
    agent.write_bytes(b"current agent")
    digest = subject._agent_digest(agent)
    profiles = infrastructure_profiles_from_doc({})
    for role, profile in profiles.items():
        profile.qualification = ImageQualification(
            baseChangeVersion="1",
            windowsBuild=26100,
            runnerVersion="2",
            agentSha256=("a" * 64 if role == "rootCa" else digest),
            validatedAt=1,
            mlDsa87Available=True,
            systemContextValidated=True,
            timeSynchronized=True,
            windowsUpdatesCurrent=True,
            backendCallbackReachable=True,
        )
    monkeypatch.setattr(subject.settings, "executor_agent_path", str(agent))
    monkeypatch.setattr(subject.settings, "backend_public_url", "https://pki.example")
    monkeypatch.setattr(subject.transport._client, "ping", lambda: True)

    result = subject.preflight_control_plane(
        profiles, mongo_ready=True, check_worker=False
    )

    check = next(item for item in result.checks if item.key == "agentBinary")
    assert check.ok is False
    assert result.agent_sha256 == digest
    assert f"Bundled agent SHA-256 is {digest}" in check.detail
    assert f"rootCa={'a' * 64}" in check.detail
    assert "domainController" not in check.detail
