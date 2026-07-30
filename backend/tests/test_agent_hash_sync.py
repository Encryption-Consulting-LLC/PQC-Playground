"""Bundled executor digest synchronization for saved settings."""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.db.client import _profiles_with_agent_hash  # noqa: E402


def test_profiles_with_agent_hash_updates_existing_qualifications_only():
    digest = "9" * 64
    profiles, changed = _profiles_with_agent_hash(
        [
            {
                "role": "domainController",
                "qualification": {
                    "agentSha256": "a" * 64,
                    "baseChangeVersion": "7",
                },
            },
            {"role": "rootCa", "qualification": None},
            {"role": "issuingCa"},
            {
                "role": "webServer",
                "qualification": {
                    "agentSha256": digest,
                    "baseChangeVersion": "8",
                },
            },
        ],
        digest,
    )

    assert changed is True
    assert profiles[0]["qualification"]["agentSha256"] == digest
    assert profiles[0]["qualification"]["baseChangeVersion"] == "7"
    assert profiles[1]["qualification"] is None
    assert "qualification" not in profiles[2]
    assert profiles[3]["qualification"]["agentSha256"] == digest


def test_profiles_with_agent_hash_can_materialize_missing_qualifications():
    digest = "9" * 64
    profiles, changed = _profiles_with_agent_hash(
        [{"role": "domainController", "qualification": None}],
        digest,
        materialize_missing=True,
    )

    assert changed is True
    assert profiles[0]["qualification"]["agentSha256"] == digest
    assert profiles[0]["qualification"]["baseChangeVersion"] == "assumed-current"
    assert profiles[0]["qualification"]["systemContextValidated"] is True


def test_profiles_with_agent_hash_reports_noop_when_digest_matches():
    digest = "9" * 64
    profiles, changed = _profiles_with_agent_hash(
        [{"role": "domainController", "qualification": {"agentSha256": digest}}],
        digest,
    )

    assert changed is False
    assert profiles[0]["qualification"]["agentSha256"] == digest
