"""Domain-admin password: AD-complexity policy + at-rest encryption round-trip.
The policy mirrors ``frontend/src/lib/passwordPolicy.ts``.
"""

import os

import pytest

# The settings module fail-fasts without these; set before importing anything
# that pulls in core.secrets. A throwaway 32-byte key is fine for the test.
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.template_config import (  # noqa: E402
    decrypt_config_secrets,
    encrypt_config_secrets,
    extract_template_config,
    password_policy_errors,
    secret_config_keys,
    validate_template_config,
)


def test_password_policy_accepts_a_strong_password():
    assert password_policy_errors("Str0ng-Lab-Pass!", "dc01") == []


@pytest.mark.parametrize(
    "value",
    [
        "short1!A",  # < 12 chars
        "alllowercaseletters",  # only one class
        "Administrator-1234!",  # contains "administrator"
    ],
)
def test_password_policy_rejects_weak_passwords(value):
    assert password_policy_errors(value) != []


def test_password_policy_rejects_the_machine_name():
    assert password_policy_errors("Guest-abc12-dc01-Xy9!", "guest-abc12-dc01") != []


def test_validate_rejects_weak_dc_password():
    with pytest.raises(ValueError):
        validate_template_config(
            "domainController",
            {
                "vmName": "dc01",
                "template": "domainController",
                "domainAdminPassword": "weak",
            },
        )


def test_validate_accepts_strong_dc_password():
    validate_template_config(
        "domainController",
        {
            "vmName": "dc01",
            "template": "domainController",
            "netbiosName": "a1b2c3-ENCON",
            "domainAdminPassword": "Str0ng-Lab-Pass!",
        },
    )


def test_validate_requires_a_dc_password():
    """An *omitted* key is invisible to the params loop, so it needs its own gate:
    without one the DC promotes with an empty DSRM password and an unjoinable
    domain, failing four sequence steps deep instead of here."""

    with pytest.raises(ValueError, match="required"):
        validate_template_config(
            "domainController",
            {"vmName": "dc01", "template": "domainController"},
        )


@pytest.mark.parametrize("value", ["", "   "])
def test_validate_rejects_a_blank_dc_password(value):
    with pytest.raises(ValueError, match="required"):
        validate_template_config(
            "domainController",
            {
                "vmName": "dc01",
                "template": "domainController",
                "domainAdminPassword": value,
            },
        )


@pytest.mark.parametrize("template", ["certificateAuthority", "standalone", "cbom"])
def test_validate_accepts_bare_params_for_templates_without_required_fields(template):
    """Guards the required-field pass against over-firing: only the DC has one."""

    validate_template_config(template, {"vmName": "vm01", "template": template})


def test_validate_accepts_optional_ipv4_reverse_zone():
    validate_template_config(
        "domainController",
        {
            "vmName": "dc01",
            "template": "domainController",
            "reverseZone": "100.168.192.in-addr.arpa",
            "domainAdminPassword": "Str0ng-Lab-Pass!",
        },
    )


def test_validate_rejects_invalid_reverse_zone():
    with pytest.raises(ValueError):
        validate_template_config(
            "domainController",
            {
                "vmName": "dc01",
                "template": "domainController",
                "reverseZone": "192.168.100.0/24",
                "domainAdminPassword": "Str0ng-Lab-Pass!",
            },
        )


def test_domain_admin_password_is_a_secret_key():
    assert "domainAdminPassword" in secret_config_keys("domainController")
    assert secret_config_keys("certificateAuthority") == frozenset()


def test_encrypt_then_decrypt_round_trips_the_password():
    config = extract_template_config(
        "domainController",
        {"vmName": "dc01", "domainAdminPassword": "Str0ng-Lab-Pass!"},
    )
    stored = encrypt_config_secrets("domainController", config)
    # At rest the plaintext is gone — the value is an AES-GCM blob.
    assert stored["domainAdminPassword"] != "Str0ng-Lab-Pass!"
    assert set(stored["domainAdminPassword"]) == {"keyId", "nonce", "ciphertext"}
    # Non-secret fields pass through unchanged.
    assert stored["domainName"] == "encon.pki"

    recovered = decrypt_config_secrets("domainController", stored)
    assert recovered["domainAdminPassword"] == "Str0ng-Lab-Pass!"


def test_blank_secret_is_dropped_not_encrypted():
    config = extract_template_config("domainController", {"domainAdminPassword": ""})
    stored = encrypt_config_secrets("domainController", config)
    assert "domainAdminPassword" not in stored


def test_mldsa_ca_drops_keylength_and_hash():
    """ML-DSA-87 fixes its own key size/hash, so the RSA-shaped keyLength/
    hashAlgorithm defaults are meaningless for it — extract drops them (the
    agent's ca.install derives the real 20736/NoHash values itself). Even when
    the hidden UI fields still carry stale values, they must not be persisted."""
    config = extract_template_config(
        "certificateAuthority",
        {
            "caType": "Root",
            "keyAlgorithm": "ML-DSA-87",
            "keyLength": "2048",  # stale form state the UI hides but still sends
            "hashAlgorithm": "SHA256",
        },
    )
    assert config["keyAlgorithm"] == "ML-DSA-87"
    assert "keyLength" not in config
    assert "hashAlgorithm" not in config


def test_ca_defaults_to_mldsa_87():
    config = extract_template_config("certificateAuthority", {})
    assert config["keyAlgorithm"] == "ML-DSA-87"
    assert "keyLength" not in config
    assert "hashAlgorithm" not in config


def test_rsa_ca_keeps_keylength_and_hash():
    """RSA (and any non-ML-DSA algorithm) still carries key length + hash."""
    config = extract_template_config("certificateAuthority", {"keyAlgorithm": "RSA"})
    assert config["keyLength"] == "2048"
    assert config["hashAlgorithm"] == "SHA256"
