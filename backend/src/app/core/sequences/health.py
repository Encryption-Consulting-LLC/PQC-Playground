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
    warnings: list[str] = []

    def check(
        name: str,
        ok: bool,
        failure: str,
        detail: Any = None,
        *,
        fatal: bool = True,
        report: bool = True,
    ) -> dict[str, Any]:
        """Record one gate fact.

        ``fatal=False`` routes the message to ``warnings``, where it is reported
        without failing the gate. ``report=False`` withholds it entirely, for a
        fact fully explained by another failure already reported. Either way the
        check is marked ``advisory`` so a consumer can tell a false that failed
        the gate from one that did not.
        """
        if not ok and report:
            (failures if fatal else warnings).append(failure)
        item: dict[str, Any] = {"ok": ok}
        if not ok and not (fatal and report):
            item["advisory"] = True
        if detail is not None:
            item["detail"] = detail
        return item

    cert = results.get("certificate-health", {})
    chain_ok = _nested(cert, "chain", "ok") is True
    cdp_ok = _nested(cert, "cdp", "ok") is True
    ocsp_ok = _nested(cert, "ocsp", "ok") is True
    # A lab whose chain builds and whose base and delta CRLs both verify has
    # working revocation without the responder, so an OCSP fault is reported
    # rather than fatal -- the alternative is failing a deploy, and stranding
    # four real VMs, over a lab that is usable for everything the CRL covers.
    # With CRL retrieval also broken there is no revocation at all, and OCSP goes
    # back to being a hard gate.
    crl_fallback = chain_ok and cdp_ok
    certificate = {
        "chain": check(
            "chain",
            chain_ok,
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
            cdp_ok,
            "required base and delta CRLs were not both verified through CDP",
            cert.get("cdp"),
        ),
        "ocsp": check(
            "ocsp",
            ocsp_ok,
            "the issued probe did not receive a verified OCSP response",
            cert.get("ocsp"),
            fatal=not crl_fallback,
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
        # Not independent evidence: `cert.verify` derives freshness as
        # `chain and cdp and ocsp` -- it measures no CRL age or OCSP `producedAt`
        # at all. Reporting it whenever one of those three fails restated that
        # failure as a second, differently worded one ("the probe did not receive
        # a verified OCSP response; revocation evidence is stale or unavailable"
        # was one fault counted twice). Only speak up when it disagrees with the
        # facts it is derived from, which is the only case where it adds anything.
        "revocationFreshness": check(
            "revocationFreshness",
            _nested(cert, "revocation_freshness", "ok") is True,
            "revocation evidence is stale or unavailable",
            cert.get("revocation_freshness"),
            report=chain_ok and cdp_ok and ocsp_ok,
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

    # A revocation configuration that reads back over COM proves only that the
    # configuration exists, not that the responder can answer with it -- so this
    # gate asserts the responder's own status too, and names it.
    #
    # What it must NOT assert is an HTTP status: the agent's probe is a bare
    # `GET /ocsp` carrying no OCSP request, and Microsoft's responder answers
    # that with 500 whether or not it is healthy. Requiring 2xx of it was a
    # check no working responder could ever pass -- it fired on every single
    # deployment, and because the real OCSP fault of the day sat right beside
    # it, it read as corroboration rather than as the false alarm it was. The
    # real evidence that OCSP answers is the downstream probe's verified
    # response (`ocsp_ok` above, from `certutil -verify -urlfetch`).
    ocsp_result = results.get("ocsp-health", {})
    pki_host = runtime.ctx.pki_host or runtime.ctx.node("web").hostname
    ocsp_endpoint = f"http://{pki_host}/ocsp"
    ocsp_configured = ocsp_result.get("configured") is True
    # Per-configuration `ErrorCode` from the responder itself: 0 is healthy, and
    # anything else is the responder saying it cannot serve that CA. An agent
    # that predates this field reports nothing, so assert only what was measured.
    configurations = ocsp_result.get("configurations") or []
    error_codes = [
        c.get("errorCode")
        for c in configurations
        if isinstance(c, dict) and isinstance(c.get("errorCode"), int)
    ]
    responder_healthy = all(code == 0 for code in error_codes)
    responder = check(
        "responder",
        ocsp_configured and responder_healthy,
        f"the Online Responder at {ocsp_endpoint} reports "
        f"error code {next((hex(c) for c in error_codes if c != 0), 'unknown')}"
        if ocsp_configured
        else "Online Responder configuration is missing or unreadable",
        {
            "endpoint": ocsp_endpoint,
            "errorCodes": error_codes,
            "configurations": configurations,
        },
        fatal=not crl_fallback,
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
        "warnings": warnings,
        "checks": {
            "certificate": certificate,
            "enterprisePki": enterprise_pki,
            "caServices": ca_services,
            "ocspResponder": responder,
            "dnsPublication": dns,
            "runtimeIdentities": identities,
        },
    }
