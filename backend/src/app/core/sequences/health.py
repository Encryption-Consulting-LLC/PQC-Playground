"""Pure aggregation for the terminal two-tier PKI deployment health gate."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.sequences.model import StepRuntime

ML_DSA_87_SIGNATURE_OID = "2.16.840.1.101.3.4.3.19"

# Containers `pki.verify` reports as plain booleans and gets right. `cdp` is
# deliberately absent: it is re-derived below from the observed names.
_AGENT_CONTAINERS = (
    "nt_auth",
    "aia",
    "certification_authorities",
    "enrollment_services",
)


def _nested(result: Mapping[str, Any], *path: str) -> Any:
    value: Any = result
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _names(value: Any) -> list[str]:
    """Normalize an ``observed`` container listing to casefolded names.

    PowerShell collapses a one-element array to a bare scalar, so a container
    holding a single object arrives as ``"EC-Root-CA"`` where two arrive as a
    list. Both have to compare equal to the same expectation.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value.casefold()]
    if isinstance(value, Sequence):
        return [str(item).casefold() for item in value]
    return [str(value).casefold()]


def _cdp_container_ok(runtime: StepRuntime, observed: Mapping[str, Any]) -> bool | None:
    """Whether both CAs published a CRL under ``CN=CDP``, or None if unknowable.

    ``pki.verify`` answers this by comparing ``CN=CDP``'s one-level children
    against the two CA *common* names, which can never hold: the CDP publication
    URL nests a level deeper than AIA's (``CN=%7%8,CN=%2,CN=CDP`` against
    ``CN=%7,CN=AIA``), so those children are the CA hosts' short names and the
    common names belong to the ``cRLDistributionPoint`` leaves inside them. The
    comparison only works here, where the expected hostnames are known.

    A pre-``observed`` agent leaves it unassertable rather than falling back to
    that boolean -- reporting a container as broken on every lab ever built is
    worse than not reporting it, and ``certificate.cdp`` independently proves the
    CRLs are fetchable and fresh.
    """
    if "cdp" not in observed:
        return None
    published = _names(observed.get("cdp"))
    return all(
        runtime.ctx.node(alias).hostname.casefold() in published
        for alias in ("root", "ca")
    )


def aggregate_lab_health(
    runtime: StepRuntime, results: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Combine fresh remote facts from all four machines into one verdict."""

    failures: list[str] = []

    def check(
        name: str,
        ok: bool,
        failure: str,
        detail: Any = None,
    ) -> dict[str, Any]:
        if not ok:
            failures.append(failure)
        item: dict[str, Any] = {"ok": ok}
        if detail is not None:
            item["detail"] = detail
        return item

    cert = results.get("certificate-health", {})
    certificate = {
        "chain": check(
            "chain",
            _nested(cert, "chain", "ok") is True,
            "certificate chain did not build successfully",
        ),
        "aia": check(
            "aia",
            _nested(cert, "aia", "ok") is True,
            "root and issuing certificates were not both verified through AIA",
            cert.get("aia"),
        ),
        "cdp": check(
            "cdp",
            _nested(cert, "cdp", "ok") is True,
            "required base and delta CRLs were not both verified through CDP",
            cert.get("cdp"),
        ),
        "ocsp": check(
            "ocsp",
            _nested(cert, "ocsp", "ok") is True,
            "the issued probe did not receive a verified OCSP response",
            cert.get("ocsp"),
        ),
        "mlDsa": check(
            "mlDsa",
            _nested(cert, "ml_dsa", "ok") is True
            and _nested(cert, "ml_dsa", "expected_oid") == ML_DSA_87_SIGNATURE_OID,
            "the probe chain does not use ML-DSA-87 signatures throughout",
            cert.get("ml_dsa"),
        ),
        "validity": check(
            "validity",
            _nested(cert, "validity", "ok") is True,
            "one or more probe-chain certificates are outside their validity window",
            cert.get("certificates"),
        ),
        "revocationFreshness": check(
            "revocationFreshness",
            _nested(cert, "revocation_freshness", "ok") is True,
            "revocation evidence is stale or unavailable",
            cert.get("revocation_freshness"),
        ),
    }

    enterprise = results.get("enterprise-pki-health", {})
    observed = enterprise.get("observed") or {}
    containers = dict(enterprise.get("containers") or {})
    asserted = list(_AGENT_CONTAINERS)
    cdp_container = _cdp_container_ok(runtime, observed)
    if cdp_container is None:
        containers.pop("cdp", None)
    else:
        containers["cdp"] = cdp_container
        asserted.append("cdp")
    enterprise_pki = {
        "containers": check(
            "containers",
            all(containers.get(name) is True for name in asserted),
            "one or more AD enterprise PKI containers are unhealthy",
            {**containers, "observed": observed} if observed else containers,
        ),
        "templates": check(
            "templates",
            _nested(enterprise, "templates", "ok") is True,
            "required enrollment templates are not published",
            enterprise.get("templates"),
        ),
        "httpArtifacts": check(
            "httpArtifacts",
            _nested(enterprise, "http_artifacts", "ok") is True,
            "one or more required HTTP AIA/CDP artifacts are unavailable",
            enterprise.get("http_artifacts"),
        ),
    }

    ca_services = {}
    for role, step_id in (
        ("root", "root-ca-health"),
        ("issuing", "issuing-ca-health"),
    ):
        result = results.get(step_id, {})
        ca_services[role] = check(
            role,
            result.get("ping_ok") is True
            and str(result.get("service", "")).casefold() == "running",
            f"{role} CA service is not running and responsive",
            {"service": result.get("service"), "pingOk": result.get("ping_ok")},
        )

    ocsp_result = results.get("ocsp-health", {})
    responder = check(
        "responder",
        ocsp_result.get("configured") is True,
        "Online Responder configuration is missing or unreadable",
        ocsp_result.get("configurations"),
    )

    dns = {}
    for role, step_id in (
        ("web", "dns-health-web"),
        ("issuing", "dns-health-issuing"),
    ):
        result = results.get(step_id, {})
        dns[role] = check(
            role,
            result.get("all_verified") is True
            and result.get("ad_srv_ok") is True
            and result.get("http_ok") is True,
            f"DNS/HTTP publication verification failed from the {role} host",
            result,
        )

    identities = {}
    for role, alias in (
        ("dc", "dc"),
        ("root", "root"),
        ("issuing", "ca"),
        ("web", "web"),
    ):
        result = results.get(f"identity-{role}", {})
        expected = runtime.ctx.node(alias).hostname
        actual = str(result.get("hostname", ""))
        identities[role] = check(
            role,
            actual.casefold() == expected.casefold()
            and result.get("server") is True
            and "windows server" in str(result.get("operating_system", "")).casefold(),
            f"{role} agent is not running on expected Windows Server host {expected}",
            {
                "expectedHostname": expected,
                "hostname": actual,
                "operatingSystem": result.get("operating_system"),
                "version": result.get("version"),
            },
        )

    return {
        "healthy": not failures,
        "failures": failures,
        "checks": {
            "certificate": certificate,
            "enterprisePki": enterprise_pki,
            "caServices": ca_services,
            "ocspResponder": responder,
            "dnsPublication": dns,
            "runtimeIdentities": identities,
        },
    }
