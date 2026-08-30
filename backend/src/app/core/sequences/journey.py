"""Build the redacted certificate-journey projection returned to the canvas."""

import datetime
from typing import Any
from urllib.parse import quote

from app.core.sequences.health import ML_DSA_87_SIGNATURE_OID
from app.core.sequences.model import RunContext


def _status(result: dict[str, Any], key: str, failure: str) -> tuple[bool, str | None]:
    value = result.get(key)
    ok = isinstance(value, dict) and value.get("ok") is True
    return ok, None if ok else failure


def build_certificate_journey(
    ctx: RunContext,
    results: dict[str, dict[str, Any]],
    *,
    verified_at: str | None = None,
) -> dict[str, Any]:
    """Project raw multi-host verification facts into five understandable hops."""

    web = ctx.node("web")
    ca = ctx.node("ca")
    cert = results.get("certificate-health", {})
    health = results.get("lab-health", {})
    checked_at = verified_at or datetime.datetime.now(datetime.UTC).isoformat()
    pki_host = ctx.pki_host or web.hostname
    base = f"http://{pki_host}/CertEnroll"
    algorithm = (
        (cert.get("ml_dsa") or {}).get("algorithm")
        or (cert.get("ml_dsa") or {}).get("expected_oid")
        or ML_DSA_87_SIGNATURE_OID
    )

    def artifact_url(key: str) -> str | None:
        filename = ctx.artifacts.get(key)
        return f"{base}/{quote(filename)}" if filename else None

    aia_ok, aia_failure = _status(
        cert, "aia", "AIA could not retrieve and validate both CA certificates."
    )
    cdp_ok, cdp_failure = _status(
        cert, "cdp", "CDP could not retrieve fresh base and delta CRLs."
    )
    # The probe's missing OCSP response is a symptom; the responder's own error
    # code is the cause, and it is the difference between "look at the probe"
    # and "look at the responder". Name it whenever the responder reported one.
    #
    # Deliberately not the HTTP status: `ocsp.verify` probes with a bare
    # `GET /ocsp` and no OCSP request, which the responder answers with 500 when
    # perfectly healthy — so quoting it here named a cause that was not one.
    responder_errors = [
        c.get("errorCode")
        for c in ((results.get("ocsp-health") or {}).get("configurations") or [])
        if isinstance(c, dict) and isinstance(c.get("errorCode"), int)
    ]
    responder_error = next((code for code in responder_errors if code != 0), None)
    ocsp_ok, ocsp_failure = _status(
        cert,
        "ocsp",
        f"The responder at http://{ctx.pki_host or web.hostname}/ocsp reports "
        f"error code {hex(responder_error)}."
        if responder_error is not None
        else "The responder did not return a verified OCSP status.",
    )
    chain_ok, chain_failure = _status(
        cert, "chain", "The issued probe certificate did not build to the trusted root."
    )
    pki_dns = {"hostname": pki_host, "address": web.ip}
    ca_dns = {
        "hostname": f"{ca.hostname}.{ctx.domain_name}"
        if ctx.domain_name
        else ca.hostname,
        "address": ca.ip,
    }

    return {
        "schemaVersion": 1,
        "healthy": health.get("healthy") is True,
        "lastVerifiedAt": checked_at,
        "signatureAlgorithm": algorithm,
        "hops": [
            {
                "id": "enroll",
                "label": f"{web.hostname} probe enrolls",
                "url": f"adcs://{ca_dns['hostname']}/Workstation",
                "dns": ca_dns,
                "artifacts": ["lab-health-probe.cer"],
                "ok": chain_ok,
                "failureReason": chain_failure,
            },
            {
                "id": "issue",
                "label": f"{ca.hostname} issues",
                "url": f"adcs://{ca_dns['hostname']}/{ca.template_config.get('commonName', ca.hostname)}",
                "dns": ca_dns,
                "artifacts": ["lab-health-probe.cer"],
                "ok": chain_ok,
                "failureReason": chain_failure,
            },
            {
                "id": "aia",
                "label": "AIA builds the chain",
                "url": base,
                "dns": pki_dns,
                "artifacts": [
                    value
                    for value in (
                        artifact_url("issuing_cert_filename"),
                        artifact_url("root_cert_filename"),
                    )
                    if value
                ],
                "ok": aia_ok,
                "failureReason": aia_failure,
            },
            {
                "id": "cdp",
                "label": "CDP checks revocation",
                "url": base,
                "dns": pki_dns,
                "artifacts": [
                    value
                    for value in (
                        artifact_url("root_crl_filename"),
                        artifact_url("issuing_crl_filename"),
                        artifact_url("issuing_delta_crl_filename"),
                    )
                    if value
                ],
                "ok": cdp_ok,
                "failureReason": cdp_failure,
            },
            {
                "id": "ocsp",
                "label": "OCSP checks status",
                "url": f"http://{pki_host}/ocsp",
                "dns": pki_dns,
                "artifacts": ["verified OCSP response"],
                "ok": ocsp_ok,
                "failureReason": ocsp_failure,
            },
        ],
    }
