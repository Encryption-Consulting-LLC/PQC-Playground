"""Sequence definitions — the ordered :class:`Step` lists each plan op expands
into.

Kept declarative and free of I/O so they read like the lab guide's build order
and stay unit-testable. Slice 9 wires the **createVm provision tails** (what
each freshly-cloned VM does once its agent phones home); the richer multi-node
op sequences (domainJoin / caConnect / webServerCert) are layered on in slices
10–12, which extend this module.

Every param that reaches PowerShell is resolved from the run context here, so a
step can reference another node's real guest-namespaced hostname
(``firstboot.hostname_for``) rather than a display name.
"""

import json
from pathlib import PureWindowsPath
from urllib.parse import quote

from app.core.sequences.health import (
    ML_DSA_87_SIGNATURE_OID,
    aggregate_lab_health,
)
from app.core.sequences.model import DnsRecordContext, RunContext, Step, StepRuntime

#: Context alias keys (mirror app.core.sequences.context).
PRIMARY = "primary"
SECONDARY = "secondary"
DC = "dc"
ROOT = "root"
WEB = "web"
CA = "ca"

# --------------------------------------------------------------------------- #
# Relay scratch paths (fixed names on C:\Transfer\, the file.read/write        #
# allowlist) + the CertEnroll publication dir the CA writes cert/CRL into.     #
# --------------------------------------------------------------------------- #
_CERT_ENROLL_DIR = "C:\\Windows\\System32\\CertSrv\\CertEnroll"
_ROOT_CRT = "C:\\Transfer\\root-ca.crt"
_ROOT_CRL = "C:\\Transfer\\root-ca.crl"
_CSR = "C:\\Transfer\\IssuingCA.req"
_ISSUING_CRT = "C:\\Transfer\\IssuingCA.crt"
#: The web host's served CertEnroll dir (the file.read/write allowlist entry).
_WEB_CERTENROLL = "C:\\CertEnroll"

# Artifact relay keys (plan_runs.artifacts).
_A_ROOT_CRT = "root_crt"
_A_ROOT_CRL = "root_crl"
_A_ISSUING_CSR = "issuing_csr"
_A_ISSUING_CRT = "issuing_crt"
_A_ROOT_CERT_FILE = "root_cert_filename"
_A_ROOT_CRL_FILE = "root_crl_filename"
_A_ISSUING_CERT_FILE = "issuing_cert_filename"
_A_ISSUING_CRL_FILE = "issuing_crl_filename"
_A_ISSUING_DELTA_CRL_FILE = "issuing_delta_crl_filename"

# Service-specific transient retry windows. Side-effecting commands carrying
# these schedules are required to verify desired state before changing it.
_AD_RETRY = (10, 20, 40, 60, 90)
_DNS_RETRY = (5, 10, 20, 30, 60)
_TEMPLATE_RETRY = (15, 30, 60, 90, 120)
_ENROLL_RETRY = (10, 20, 40, 60, 90)
_CRL_RETRY = (10, 20, 40, 60)
_OCSP_RETRY = (10, 20, 40, 60, 90)

# ``Install-ADDSForest``'s prerequisite refusal when the box has a restart
# pending — the locale-independent error id first, then the English message text
# it ships with as a fallback.
_DCPROMO_REBOOT_PENDING = (
    "dcpromo.general.15",
    "needs to be restarted",
    "role change is in progress",
)


def _admin_username(netbios: str | None) -> str:
    return f"{netbios}\\Administrator" if netbios else "Administrator"


def _forest_mode(forest_level: str | None) -> str:
    """Map the template's forest-level label to the cmdlet's mode token. Only
    Windows Server 2025 introduced a new functional level past WinThreshold
    (2016), which covers 2016/2019/2022."""
    return "Win2025" if forest_level and "2025" in forest_level else "WinThreshold"


def _node_by_id(ctx: RunContext, node_id: str):
    for node in ctx.nodes.values():
        if node.node_id == node_id:
            return node
    raise KeyError(f"DNS resource references unavailable node '{node_id}'")


def _fqdn(ctx: RunContext, hostname: str) -> str:
    return f"{hostname}.{ctx.domain_name}" if ctx.domain_name else hostname


def _materialize_dns_records(
    ctx: RunContext, records: tuple[DnsRecordContext, ...]
) -> str:
    """Resolve symbolic topology DNS resources into flat agent command data."""

    materialized: list[dict[str, str]] = []
    for record in records:
        subject = _node_by_id(ctx, record.subject)
        if record.kind == "A":
            if not subject.ip:
                raise ValueError(f"A resource '{record.id}' has no allocated address")
            name, value = subject.hostname, subject.ip
        elif record.kind == "PTR":
            if not subject.ip:
                raise ValueError(f"PTR resource '{record.id}' has no allocated address")
            name, value = subject.ip, f"{_fqdn(ctx, subject.hostname)}."
        elif record.kind == "CNAME":
            name, value = record.name or "", f"{_fqdn(ctx, subject.hostname)}."
        else:
            raise ValueError(f"unsupported DNS resource kind '{record.kind}'")
        materialized.append(
            {
                "id": record.id,
                "kind": record.kind,
                "zone": record.zone.rstrip("."),
                "name": name,
                "value": value,
            }
        )
    return json.dumps(materialized, separators=(",", ":"), sort_keys=True)


def _dns_params(
    ctx: RunContext,
    records: tuple[DnsRecordContext, ...],
    *,
    require_ad_srv: bool = False,
    http_url: str | None = None,
) -> dict[str, str]:
    server = _node_by_id(ctx, records[0].server)
    params = {
        "records": _materialize_dns_records(ctx, records),
        "server": server.ip or server.hostname,
    }
    if require_ad_srv:
        params["requireAdSrv"] = "true"
        params["domain"] = ctx.domain_name or ""
    if http_url:
        params["httpUrl"] = http_url
    return params


def _records_for(
    ctx: RunContext, subject_id: str, kinds: tuple[str, ...]
) -> tuple[DnsRecordContext, ...]:
    return tuple(
        record
        for record in ctx.dns_records
        if record.subject == subject_id and record.kind in kinds
    )


def _ds_config_dn(domain: str) -> str:
    """`CN=Configuration,DC=encon,DC=pki` from the domain — the
    lab's DSConfigDN string baked into issued certs."""
    dc = ",".join(f"DC={part}" for part in domain.split(".") if part)
    return f"CN=Configuration,{dc}"


def _observed_filename(ctx: RunContext, key: str) -> str:
    """Return a certutil-observed leaf filename persisted by publication."""

    value = ctx.artifacts.get(key)
    if not value:
        raise ValueError(f"CA publication did not report artifact '{key}'")
    leaf = PureWindowsPath(value).name
    if leaf != value or leaf in ("", ".", ".."):
        raise ValueError(f"CA publication reported unsafe filename '{value}'")
    return leaf


def _observed_filename_or(ctx: RunContext, key: str, fallback: str) -> str:
    """Return a safe observed filename, or a deterministic legacy fallback.

    Older/lost plan state may contain the CA files on disk without retaining
    the filename artifacts.  The fallback mirrors AD CS publication naming so
    a reconcile can rehydrate the binary relay from the root CA.
    """

    value = ctx.artifacts.get(key) or fallback
    leaf = PureWindowsPath(value).name
    if leaf != value or leaf in ("", ".", ".."):
        raise ValueError(f"CA publication reported unsafe filename '{value}'")
    return leaf


def _publication_url(ctx: RunContext, key: str) -> str:
    return quote(_observed_filename(ctx, key), safe="+._-")


def _ca_publication_dir(rt: StepRuntime) -> str:
    return rt.node.template_config.get("certEnrollPath", _CERT_ENROLL_DIR)


def _root_aia(pki_host: str, publication_dir: str = _CERT_ENROLL_DIR) -> str:
    return "\n".join(
        [
            f"1:{publication_dir}\\%1_%3%4.crt",
            "2:ldap:///CN=%7,CN=AIA,CN=Public Key Services,CN=Services,%6%11",
            f"2:http://{pki_host}/CertEnroll/%1_%3%4.crt",
        ]
    )


def _root_cdp(pki_host: str, publication_dir: str = _CERT_ENROLL_DIR) -> str:
    return "\n".join(
        [
            f"1:{publication_dir}\\%3%8%9.crl",
            "10:ldap:///CN=%7%8,CN=%2,CN=CDP,CN=Public Key Services,CN=Services,%6%10",
            f"2:http://{pki_host}/CertEnroll/%3%8%9.crl",
        ]
    )


def _issuing_aia(
    pki_host: str,
    ocsp_host: str,
    publication_dir: str = _CERT_ENROLL_DIR,
) -> str:
    return "\n".join(
        [
            f"1:{publication_dir}\\%1_%3%4.crt",
            "2:ldap:///CN=%7,CN=AIA,CN=Public Key Services,CN=Services,%6%11",
            f"2:http://{pki_host}/CertEnroll/%1_%3%4.crt",
            f"32:http://{ocsp_host}/ocsp",
        ]
    )


def _issuing_cdp(
    pki_host: str,
    unc_host: str,
    publication_dir: str = _CERT_ENROLL_DIR,
) -> str:
    return "\n".join(
        [
            f"65:{publication_dir}\\%3%8%9.crl",
            "79:ldap:///CN=%7%8,CN=%2,CN=CDP,CN=Public Key Services,CN=Services,%6%10",
            f"6:http://{pki_host}/CertEnroll/%3%8%9.crl",
            f"65:\\\\{unc_host}\\CertEnroll\\%3%8%9.crl",
        ]
    )


def _ca_verify_step() -> Step:
    """`ca.verify` probe — the CA is ready once `certutil -ping` answers."""
    return Step(id="ca-verify", command="ca.verify", target=PRIMARY)


def _ca_install_params(rt: StepRuntime) -> dict[str, str]:
    """Root-CA install params straight from the node's template config
    (caType/commonName/keyAlgorithm/…). The config was validated + defaulted by
    ``extract_template_config`` and decrypted by the dispatch path."""
    cfg = rt.node.template_config
    return {k: v for k, v in cfg.items() if v not in (None, "")}


def _root_ca_provision() -> list[Step]:
    """Offline root CA (createVm tail, slice 11): install the standalone root,
    apply the lab's registry settings, set the 3-location root CDP/AIA arrays,
    publish the first CRL, then read the root cert + CRL into the relay so the
    caConnect handshake can carry them to CA02/SRV1/DC01. Domain facts
    (DSConfigDN, the pki HTTP host) come from the plan's DC via the run context
    — the root is offline but its issued-cert URLs still hard-code the domain.
    """

    def settings_params(rt: StepRuntime) -> dict[str, str]:
        params = {
            "crlPeriodUnits": "52",
            "crlPeriod": "Weeks",
            "crlDeltaPeriodUnits": "0",
            "crlOverlapUnits": "12",
            "crlOverlapPeriod": "Hours",
            "validityPeriodUnits": "10",
            "validityPeriod": "Years",
            "auditFilter": "127",
        }
        if rt.ctx.domain_name:
            params["dsConfigDn"] = _ds_config_dn(rt.ctx.domain_name)
        return params

    def cdp_aia_params(rt: StepRuntime) -> dict[str, str]:
        pki = rt.ctx.pki_host or "pki.local"
        publication_dir = _ca_publication_dir(rt)
        return {
            "aiaUrls": _root_aia(pki, publication_dir),
            "cdpUrls": _root_cdp(pki, publication_dir),
        }

    def root_crt_path(rt: StepRuntime) -> dict[str, str]:
        return {
            "path": f"{_ca_publication_dir(rt)}\\{_observed_filename(rt.ctx, _A_ROOT_CERT_FILE)}"
        }

    def root_crl_path(rt: StepRuntime) -> dict[str, str]:
        return {
            "path": f"{_ca_publication_dir(rt)}\\{_observed_filename(rt.ctx, _A_ROOT_CRL_FILE)}"
        }

    def publication_defaults(rt: StepRuntime) -> dict[str, str]:
        ca_name = rt.node.template_config.get("commonName", "")
        return {
            "certificateFileName": f"{rt.node.hostname}_{ca_name}.crt",
            "baseCrlFileName": f"{ca_name}.crl",
        }

    return [
        Step(
            id="ca-install",
            command="ca.install",
            target=PRIMARY,
            params=_ca_install_params,
            verify=_ca_verify_step(),
            verify_predicate=lambda r: r.get("ping_ok") is True,
            timeout_s=1800,
        ),
        Step(
            id="ca-settings",
            command="ca.configure_settings",
            target=PRIMARY,
            params=settings_params,
        ),
        Step(
            id="ca-cdp-aia",
            command="ca.configure_cdp_aia",
            target=PRIMARY,
            params=cdp_aia_params,
        ),
        Step(
            id="ca-crl",
            command="ca.publish_crl",
            target=PRIMARY,
            params=lambda rt: {"certEnrollPath": _ca_publication_dir(rt)},
            retry_delays_s=_CRL_RETRY,
            result_artifacts={
                "certificateFileName": _A_ROOT_CERT_FILE,
                "baseCrlFileName": _A_ROOT_CRL_FILE,
            },
            result_artifact_defaults=publication_defaults,
            optional_result_artifacts={
                "certificateContentB64": _A_ROOT_CRT,
                "baseCrlContentB64": _A_ROOT_CRL,
            },
        ),
        Step(
            id="read-root-crt",
            command="file.read",
            target=PRIMARY,
            params=root_crt_path,
            produces=(_A_ROOT_CRT,),
            skip_if_artifacts=(_A_ROOT_CRT,),
        ),
        Step(
            id="read-root-crl",
            command="file.read",
            target=PRIMARY,
            params=root_crl_path,
            produces=(_A_ROOT_CRL,),
            skip_if_artifacts=(_A_ROOT_CRL,),
        ),
    ]


def _domain_controller_provision(*, include_dns: bool = False) -> list[Step]:
    """Domain controller (createVm tail, slice 11): promote a new forest
    (``Install-ADDSForest -NoRebootOnCompletion``), reboot, point the DC's own
    NIC DNS at itself, then verify ADWS is up. DNS must precede the probe so
    legacy agents that use default DC discovery do not retain the cloned
    image's pre-promotion resolver settings. The `pki` CNAME is deferred to
    the webServerCert op (it only resolves usefully once the web host exists).

    ``dc.install_forest`` installs the AD DS role and promotes in one PowerShell
    session, discarding ``Install-WindowsFeature``'s ``RestartNeeded`` — so a role
    install (or any pending state inherited from firstboot) that wants a restart
    makes the promotion's prerequisite check refuse. Rebooting and redispatching
    clears it: the feature install is a no-op the second time and the promotion
    then runs on a clean boot."""

    def forest_params(rt: StepRuntime) -> dict[str, str]:
        cfg = rt.node.template_config
        return {
            "domainName": cfg.get("domainName", ""),
            "netbiosName": cfg.get("netbiosName", ""),
            "forestMode": _forest_mode(cfg.get("forestLevel")),
            "safeModePassword": cfg.get("domainAdminPassword", ""),
        }

    steps = [
        Step(
            id="install-forest",
            command="dc.install_forest",
            target=PRIMARY,
            params=forest_params,
            secret_keys=("safeModePassword",),
            timeout_s=1800,
            reboot_recovery_signatures=_DCPROMO_REBOOT_PENDING,
        ),
        Step(
            id="reboot",
            command="system.reboot",
            target=PRIMARY,
            expects_disconnect=True,
            timeout_s=1200,
        ),
        Step(
            id="dns-self",
            command="dns.set_client",
            target=PRIMARY,
            params=lambda rt: {"servers": rt.node.ip or "127.0.0.1"},
            verify=Step(id="dc-verify", command="dc.verify", target=PRIMARY),
            verify_predicate=lambda r: bool((r.get("domain") or {}).get("DNSRoot")),
            verify_window_s=900,
        ),
    ]
    if include_dns:

        def records(rt: StepRuntime) -> tuple[DnsRecordContext, ...]:
            return _records_for(rt.ctx, rt.node.node_id, ("A", "PTR"))

        steps.extend(
            [
                Step(
                    id="dns-apply",
                    command="dns.apply_resources",
                    target=PRIMARY,
                    params=lambda rt: _dns_params(rt.ctx, records(rt)),
                    retry_delays_s=_DNS_RETRY,
                ),
                Step(
                    id="dns-verify",
                    command="dns.verify",
                    target=PRIMARY,
                    params=lambda rt: _dns_params(
                        rt.ctx, records(rt), require_ad_srv=True
                    ),
                    retry_delays_s=_DNS_RETRY,
                ),
            ]
        )
    return steps


#: createVm provision tails by template id. A template absent here provisions
#: nothing on first boot (its role is driven later by plan ops) — a member
#: server does its work on domain join (slice 10).
_PROVISION_SEQUENCES = {
    "domainController": _domain_controller_provision,
    "certificateAuthority": _root_ca_provision,
}


def provision_steps(
    template: str,
    *,
    ca_type: str | None = None,
    node_id: str | None = None,
    dns_records: tuple[DnsRecordContext, ...] = (),
) -> list[Step]:
    """The createVm provision tail for ``template`` (empty when there's none).

    ``ca_type`` skips provisioning an *issuing* CA on first boot — it can't
    stand up until the caConnect handshake, so its createVm tail is empty and
    the work happens in that op.
    """
    if template == "certificateAuthority" and ca_type == "Issuing":
        return []
    if template == "domainController":
        include_dns = bool(
            node_id
            and any(
                record.subject == node_id and record.kind in ("A", "PTR")
                for record in dns_records
            )
        )
        return _domain_controller_provision(include_dns=include_dns)
    builder = _PROVISION_SEQUENCES.get(template)
    return builder() if builder else []


def _domain_join_sequence(ctx: RunContext) -> list[Step]:
    """Join the target node to the forest (slice 10).

    Point DNS at the DC, ``Add-Computer`` under the operator's domain-admin
    credential (no ``-Restart``), reboot, then verify membership. A web-server
    target additionally gets the CertEnroll *share/ACL* half here (it needs the
    domain's Cert Publishers group, and must exist before the root cert is
    published to it) — the IIS/vdir half runs in the webServerCert op.
    """
    dc = ctx.node(DC)

    def join_params(rt: StepRuntime) -> dict[str, str]:
        return {
            "domainName": ctx.domain_name or "",
            "username": _admin_username(ctx.netbios),
            "password": dc.template_config.get("domainAdminPassword", ""),
        }

    steps = [
        Step(
            id="dns-set",
            command="dns.set_client",
            target=PRIMARY,
            params=lambda rt: {"servers": ctx.node(DC).ip or ""},
        ),
        Step(
            id="domain-join",
            command="domain.join",
            target=PRIMARY,
            params=join_params,
            secret_keys=("password",),
            retry_delays_s=_AD_RETRY,
        ),
        # The join reboots (Add-Computer without -Restart, then a reboot step);
        # domain.verify — retried post-reboot until PartOfDomain — is its gate.
        Step(
            id="reboot",
            command="system.reboot",
            target=PRIMARY,
            expects_disconnect=True,
            timeout_s=1200,
            verify=Step(id="domain-verify", command="domain.verify", target=PRIMARY),
            verify_predicate=lambda r: r.get("part_of_domain") is True,
            verify_window_s=600,
        ),
    ]

    dns_records = _records_for(ctx, ctx.node(PRIMARY).node_id, ("A", "PTR"))
    if dns_records:
        steps.extend(
            [
                Step(
                    id="dns-apply",
                    command="dns.apply_resources",
                    target=DC,
                    params=lambda rt: _dns_params(ctx, dns_records),
                    retry_delays_s=_DNS_RETRY,
                ),
                Step(
                    id="dns-verify",
                    command="dns.verify",
                    target=PRIMARY,
                    params=lambda rt: _dns_params(ctx, dns_records),
                    timeout_s=300,
                    retry_delays_s=_DNS_RETRY,
                ),
            ]
        )

    if ctx.node(PRIMARY).template_id == "webServer":
        steps.append(
            Step(
                id="iis-share",
                command="iis.setup_certenroll",
                target=PRIMARY,
                params=lambda rt: {
                    "scope": "share",
                    "netbiosName": ctx.netbios or "",
                    "path": rt.node.template_config.get(
                        "certEnrollPath", "C:\\CertEnroll"
                    ),
                },
            )
        )

    # Client enrollment rides the join (slice 12): once a client is in the
    # domain and an issuing CA has published the Workstation template, enroll a
    # cert, export it, and run the lab's own chain+revocation check — the Deploy
    # ends at the verified chain. Gated on an issuing CA being present.
    if ctx.node(PRIMARY).template_id == "client" and CA in ctx.nodes:
        steps.append(
            Step(
                id="enroll-workstation",
                command="cert.enroll",
                target=PRIMARY,
                params={
                    "template": "Workstation",
                    "exportPath": "C:\\win11.cer",
                    "refreshPolicy": "true",
                },
                verify=Step(
                    id="cert-verify",
                    command="cert.verify",
                    target=PRIMARY,
                    params={"path": "C:\\win11.cer"},
                ),
                verify_predicate=lambda r: r.get("chain_ok") is True,
                verify_window_s=900,
                retry_delays_s=_ENROLL_RETRY,
            )
        )
    return steps


def _domain_leave_sequence(ctx: RunContext) -> list[Step]:
    """Leave the forest, reboot, and remove only topology-owned DNS records."""

    dc = ctx.node(DC)

    def leave_params(rt: StepRuntime) -> dict[str, str]:
        return {
            "workgroup": "WORKGROUP",
            "username": _admin_username(ctx.netbios),
            "password": dc.template_config.get("domainAdminPassword", ""),
        }

    steps = [
        Step(
            id="domain-leave",
            command="domain.leave",
            target=PRIMARY,
            params=leave_params,
            secret_keys=("password",),
            retry_delays_s=_AD_RETRY,
        ),
        Step(
            id="reboot",
            command="system.reboot",
            target=PRIMARY,
            expects_disconnect=True,
            timeout_s=1200,
            verify=Step(id="domain-verify", command="domain.verify", target=PRIMARY),
            verify_predicate=lambda result: result.get("part_of_domain") is False,
            verify_window_s=600,
        ),
    ]
    records = _records_for(ctx, ctx.node(PRIMARY).node_id, ("A", "PTR", "CNAME"))
    if records:
        steps.extend(
            [
                Step(
                    id="dns-remove",
                    command="dns.remove_resources",
                    target=DC,
                    params=lambda rt: _dns_params(ctx, records),
                    retry_delays_s=_DNS_RETRY,
                ),
                Step(
                    id="dns-verify-absent",
                    command="dns.verify_absent",
                    target=DC,
                    params=lambda rt: _dns_params(ctx, records),
                    retry_delays_s=_DNS_RETRY,
                ),
            ]
        )
    return steps


def teardown_action_sequence(kind: str, ctx: RunContext) -> list[Step]:
    """Expand one compiled teardown action into convergent agent commands."""

    if kind == "domain.leave":
        return _domain_leave_sequence(ctx)
    if kind == "web.cleanup":
        return [
            Step(
                id="ocsp-remove",
                command="ocsp.remove",
                target=PRIMARY,
                retry_delays_s=_OCSP_RETRY,
            ),
            Step(
                id="iis-remove",
                command="iis.remove_certenroll",
                target=PRIMARY,
                retry_delays_s=_AD_RETRY,
            ),
        ]
    if kind == "ca.cleanup":
        return [
            Step(
                id="ca-uninstall",
                command="ca.uninstall",
                target=PRIMARY,
                retry_delays_s=_AD_RETRY,
            )
        ]
    if kind == "dns.cleanup":
        records = tuple(ctx.dns_records)
        if not records:
            return []
        return [
            Step(
                id="dns-remove-all",
                command="dns.remove_resources",
                target=PRIMARY,
                params=lambda rt: _dns_params(ctx, records),
                retry_delays_s=_DNS_RETRY,
            ),
            Step(
                id="dns-verify-all-absent",
                command="dns.verify_absent",
                target=PRIMARY,
                params=lambda rt: _dns_params(ctx, records),
                retry_delays_s=_DNS_RETRY,
            ),
        ]
    if kind == "forest.cleanup":
        password = ctx.node(PRIMARY).template_config.get("domainAdminPassword", "")
        return [
            Step(
                id="forest-remove",
                command="dc.remove_forest",
                target=PRIMARY,
                params={"localAdminPassword": password},
                secret_keys=("localAdminPassword",),
                timeout_s=1800,
                retry_delays_s=_AD_RETRY,
            )
        ]
    return []


def _web_server_cert_sequence(ctx: RunContext) -> list[Step]:
    """Stand up the web host's HTTP CDP/AIA + Online Responder (webServerCert,
    slice 12).

    The IIS/vdir half of CertEnroll, then the Online Responder role, the OCSP
    signing cert (auto-enrolled off the template caConnect published + granted),
    the revocation configuration pointed at the issuing CA's CRLs (the CertAdm
    COM canary), and a responder self-check. Finally the deferred ``pki`` CNAME
    on the DC — created here (not in the DC's tail) because it only resolves
    usefully once this web host exists.
    """
    ca = ctx.nodes.get(CA)
    root = ctx.nodes.get(ROOT)
    issuing_cn = (
        ca.template_config.get("commonName", "Issuing CA") if ca else "Issuing CA"
    )
    root_cn = (
        root.template_config.get("commonName", "EC-Root-CA") if root else "EC-Root-CA"
    )
    ca_config = (
        f"{ca.hostname}.{ctx.domain_name}\\{issuing_cn}"
        if ca and ctx.domain_name
        else ""
    )
    pki = ctx.pki_host or "pki.local"
    refresh = ctx.node(PRIMARY).template_config.get("ocspRefreshMinutes", "15")

    steps = [
        Step(
            id="iis-web",
            command="iis.setup_certenroll",
            target=PRIMARY,
            params=lambda rt: {
                "scope": "web",
                "path": rt.node.template_config.get("certEnrollPath", "C:\\CertEnroll"),
            },
        ),
        Step(
            id="ocsp-install",
            command="ocsp.install",
            target=PRIMARY,
            timeout_s=900,
            retry_delays_s=_OCSP_RETRY,
        ),
        Step(
            id="enroll-ocsp",
            command="cert.enroll",
            target=PRIMARY,
            params={
                "template": "OCSPResponseSigning",
                "refreshPolicy": "true",
            },
            verify_window_s=900,
            retry_delays_s=_ENROLL_RETRY,
        ),
        Step(
            id="ocsp-config",
            command="ocsp.configure_revocation",
            target=PRIMARY,
            params={
                "name": "EC-Issuing-CA",
                "caConfig": ca_config,
                "template": "OCSPResponseSigning",
                "refreshMinutes": refresh,
                "baseCrlUrls": (
                    f"http://{pki}/CertEnroll/"
                    f"{_publication_url(ctx, _A_ISSUING_CRL_FILE)}"
                ),
                "deltaCrlUrls": (
                    f"http://{pki}/CertEnroll/"
                    f"{_publication_url(ctx, _A_ISSUING_DELTA_CRL_FILE)}"
                ),
            },
            verify=Step(id="ocsp-verify", command="ocsp.verify", target=PRIMARY),
            verify_predicate=lambda r: r.get("configured") is True,
            verify_window_s=600,
            retry_delays_s=_OCSP_RETRY,
        ),
    ]

    # Apply the planned CNAME on the authoritative DC, then prove both name
    # resolution and the CertEnroll HTTP hop from the web host and issuing CA.
    cname_records = _records_for(ctx, ctx.node(PRIMARY).node_id, ("CNAME",))
    if cname_records and DC in ctx.nodes:
        steps.append(
            Step(
                id="dns-cname-apply",
                command="dns.apply_resources",
                target=DC,
                params=lambda rt: _dns_params(ctx, cname_records),
                retry_delays_s=_DNS_RETRY,
            )
        )
        http_url = f"http://{ctx.pki_host}/CertEnroll/"
        for target, suffix in ((PRIMARY, "web"), (CA, "ca")):
            if target not in ctx.nodes:
                continue
            steps.append(
                Step(
                    id=f"dns-cname-verify-{suffix}",
                    command="dns.verify",
                    target=target,
                    params=lambda rt, url=http_url: _dns_params(
                        ctx, cname_records, http_url=url
                    ),
                    timeout_s=300,
                    retry_delays_s=_DNS_RETRY,
                )
            )
    steps.append(
        Step(
            id="enroll-health-probe",
            command="cert.enroll",
            target=PRIMARY,
            params={
                "template": "Workstation",
                "exportPath": "C:\\Transfer\\lab-health-probe.cer",
                "refreshPolicy": "true",
            },
            timeout_s=900,
            retry_delays_s=_ENROLL_RETRY,
        )
    )

    if not all(alias in ctx.nodes for alias in (DC, ROOT, CA, WEB)):
        raise KeyError("final lab health gate requires dc, root, ca, and web nodes")

    root_cert_name = _observed_filename(ctx, _A_ROOT_CERT_FILE)
    issuing_cert_name = _observed_filename(ctx, _A_ISSUING_CERT_FILE)
    http_root = f"http://{pki}/CertEnroll/"
    artifact_urls = [
        f"{http_root}{_publication_url(ctx, _A_ROOT_CERT_FILE)}",
        f"{http_root}{_publication_url(ctx, _A_ROOT_CRL_FILE)}",
        f"{http_root}{_publication_url(ctx, _A_ISSUING_CERT_FILE)}",
        f"{http_root}{_publication_url(ctx, _A_ISSUING_CRL_FILE)}",
        f"{http_root}{_publication_url(ctx, _A_ISSUING_DELTA_CRL_FILE)}",
    ]
    all_dns_records = tuple(ctx.dns_records)
    if not all_dns_records:
        raise ValueError("final lab health gate requires planned DNS resources")
    dns_params = lambda rt: _dns_params(  # noqa: E731 - declarative resolver
        ctx,
        all_dns_records,
        require_ad_srv=True,
        http_url=http_root,
    )

    steps += [
        Step(
            id="certificate-health",
            command="cert.verify",
            target=WEB,
            params={
                "path": "C:\\Transfer\\lab-health-probe.cer",
                "rootPath": f"{_WEB_CERTENROLL}\\{root_cert_name}",
                "issuingPath": f"{_WEB_CERTENROLL}\\{issuing_cert_name}",
                "expectedSignatureOid": ML_DSA_87_SIGNATURE_OID,
            },
            timeout_s=900,
        ),
        Step(
            id="enterprise-pki-health",
            command="pki.verify",
            target=DC,
            params={
                "rootCaCommonName": root_cn,
                "issuingCaCommonName": issuing_cn,
                "templates": "OCSPResponseSigning,Workstation",
                "httpUrls": json.dumps(artifact_urls, separators=(",", ":")),
            },
            timeout_s=600,
        ),
        Step(id="root-ca-health", command="ca.verify", target=ROOT),
        Step(id="issuing-ca-health", command="ca.verify", target=CA),
        Step(id="ocsp-health", command="ocsp.verify", target=WEB),
        Step(
            id="dns-health-web",
            command="dns.verify",
            target=WEB,
            params=dns_params,
            retry_delays_s=_DNS_RETRY,
        ),
        Step(
            id="dns-health-issuing",
            command="dns.verify",
            target=CA,
            params=dns_params,
            retry_delays_s=_DNS_RETRY,
        ),
        Step(id="identity-dc", command="system.identity", target=DC),
        Step(id="identity-root", command="system.identity", target=ROOT),
        Step(id="identity-issuing", command="system.identity", target=CA),
        Step(id="identity-web", command="system.identity", target=WEB),
        Step(
            id="lab-health",
            command="lab.verify",
            target=WEB,
            aggregate=aggregate_lab_health,
        ),
    ]
    return steps


def _web_fqdn(ctx: RunContext) -> str | None:
    web = ctx.nodes.get(WEB)
    if web is None or not ctx.domain_name:
        return None
    return f"{web.hostname}.{ctx.domain_name}"


def _ca_connect_sequence(ctx: RunContext) -> list[Step]:
    """The two-tier cross-sign handshake (caConnect, slice 11).

    Carries the root cert/CRL (produced into the relay by the root CA's
    createVm tail) to CA02 + AD + the web host, stands the issuing CA up on
    CA02 under the domain-admin credential, relays its CSR out to the offline
    root and the signed cert back, finishes CA02's config, and publishes the
    OCSP + Workstation templates (granting the web host enroll rights). Every
    cross-VM hop goes through the file.read/write relay — the sneakernet — so
    no SMB path is needed for the handshake itself.

    Requires the full topology: the ``root`` (secondary), ``dc`` and ``web``
    nodes must resolve. Steps targeting the web/DC degrade out only where the
    lab allows (they don't here — a UNC CDP and template grant genuinely need
    those hosts), so a missing node raises and fails the op.
    """
    root = ctx.node(ROOT)
    has_web = WEB in ctx.nodes
    has_dc = DC in ctx.nodes
    pki = ctx.pki_host or "pki.local"
    web_fqdn = _web_fqdn(ctx)

    def issuing_install_params(rt: StepRuntime) -> dict[str, str]:
        cfg = dict(rt.node.template_config)
        cfg["caType"] = "Issuing"
        cfg["csrPath"] = _CSR
        dc = ctx.nodes.get(DC)
        cfg["username"] = _admin_username(ctx.netbios)
        cfg["password"] = (
            dc.template_config.get("domainAdminPassword", "") if dc else ""
        )
        return {k: v for k, v in cfg.items() if v not in (None, "")}

    def issuing_settings_params(rt: StepRuntime) -> dict[str, str]:
        return {
            "crlPeriodUnits": "1",
            "crlPeriod": "Weeks",
            "crlDeltaPeriodUnits": "1",
            "crlDeltaPeriod": "Days",
            "crlOverlapUnits": "12",
            "crlOverlapPeriod": "Hours",
            "validityPeriodUnits": "5",
            "validityPeriod": "Years",
            "auditFilter": "127",
        }

    def issuing_cdp_aia_params(rt: StepRuntime) -> dict[str, str]:
        publication_dir = _ca_publication_dir(rt)
        aia = (
            _issuing_aia(pki, web_fqdn, publication_dir)
            if web_fqdn
            else _root_aia(pki, publication_dir)
        )
        cdp = (
            _issuing_cdp(pki, web_fqdn, publication_dir)
            if web_fqdn
            else _root_cdp(pki, publication_dir)
        )
        return {"aiaUrls": aia, "cdpUrls": cdp}

    def issuing_pub_crt_path(rt: StepRuntime) -> dict[str, str]:
        return {
            "path": f"{_ca_publication_dir(rt)}\\"
            f"{_observed_filename(rt.ctx, _A_ISSUING_CERT_FILE)}"
        }

    def root_source_crt_path(rt: StepRuntime) -> dict[str, str]:
        ca_name = rt.node.template_config.get("commonName", "EC-Root-CA")
        filename = _observed_filename_or(
            rt.ctx,
            _A_ROOT_CERT_FILE,
            f"{rt.node.hostname}_{ca_name}.crt",
        )
        return {"path": f"{_ca_publication_dir(rt)}\\{filename}"}

    def root_source_crl_path(rt: StepRuntime) -> dict[str, str]:
        ca_name = rt.node.template_config.get("commonName", "EC-Root-CA")
        filename = _observed_filename_or(
            rt.ctx,
            _A_ROOT_CRL_FILE,
            f"{ca_name}.crl",
        )
        return {"path": f"{_ca_publication_dir(rt)}\\{filename}"}

    def root_crt_recovery_defaults(rt: StepRuntime) -> dict[str, str]:
        ca_name = rt.node.template_config.get("commonName", "EC-Root-CA")
        return {
            "sourceFileName": _observed_filename_or(
                rt.ctx,
                _A_ROOT_CERT_FILE,
                f"{rt.node.hostname}_{ca_name}.crt",
            )
        }

    def root_crl_recovery_defaults(rt: StepRuntime) -> dict[str, str]:
        ca_name = rt.node.template_config.get("commonName", "EC-Root-CA")
        return {
            "sourceFileName": _observed_filename_or(
                rt.ctx,
                _A_ROOT_CRL_FILE,
                f"{ca_name}.crl",
            )
        }

    def root_web_crt_path(rt: StepRuntime) -> dict[str, str]:
        return {
            "path": f"{_WEB_CERTENROLL}\\{_observed_filename(rt.ctx, _A_ROOT_CERT_FILE)}"
        }

    def root_web_crl_path(rt: StepRuntime) -> dict[str, str]:
        return {
            "path": f"{_WEB_CERTENROLL}\\{_observed_filename(rt.ctx, _A_ROOT_CRL_FILE)}"
        }

    def issuing_web_crt_path(rt: StepRuntime) -> dict[str, str]:
        return {
            "path": f"{_WEB_CERTENROLL}\\{_observed_filename(rt.ctx, _A_ISSUING_CERT_FILE)}"
        }

    def issuing_publication_defaults(rt: StepRuntime) -> dict[str, str]:
        ca_name = rt.node.template_config.get("commonName", "")
        server_dns_name = _fqdn(rt.ctx, rt.node.hostname)
        return {
            "certificateFileName": f"{server_dns_name}_{ca_name}.crt",
            "baseCrlFileName": f"{ca_name}.crl",
            "deltaCrlFileName": f"{ca_name}+.crl",
        }

    steps: list[Step] = []

    # 1) Rehydrate a missing relay from the offline root's configured
    #    publication directory, then trust the root on CA02 — *both* its cert
    #    and its CRL. `certutil -installcert` (step 4) validates the whole
    #    chain, so a missing local CRL fails it with CRYPT_E_REVOCATION_OFFLINE:
    #    the offline root's CDP is an LDAP/HTTP location that nothing serves yet
    #    at this point in the plan, and the chain engine has no other copy.
    #    The recovery reads normally skip, but make retry/reconcile self-healing
    #    when an older parallel worker erased plan artifacts after root
    #    provisioning.
    steps += [
        Step(
            id="recover-root-crt",
            command="file.read",
            target=ROOT,
            params=root_source_crt_path,
            produces=(_A_ROOT_CRT,),
            result_artifacts={"sourceFileName": _A_ROOT_CERT_FILE},
            result_artifact_defaults=root_crt_recovery_defaults,
            skip_if_artifacts=(_A_ROOT_CRT, _A_ROOT_CERT_FILE),
        ),
        Step(
            id="recover-root-crl",
            command="file.read",
            target=ROOT,
            params=root_source_crl_path,
            produces=(_A_ROOT_CRL,),
            result_artifacts={"sourceFileName": _A_ROOT_CRL_FILE},
            result_artifact_defaults=root_crl_recovery_defaults,
            skip_if_artifacts=(_A_ROOT_CRL, _A_ROOT_CRL_FILE),
        ),
        Step(
            id="root-to-ca02",
            command="file.write",
            target=PRIMARY,
            params={"path": _ROOT_CRT},
            consumes=(_A_ROOT_CRT,),
        ),
        Step(
            id="addstore-root",
            command="cert.addstore",
            target=PRIMARY,
            params={"store": "root", "path": _ROOT_CRT, "kind": "cert"},
        ),
        Step(
            id="rootcrl-to-ca02",
            command="file.write",
            target=PRIMARY,
            params={"path": _ROOT_CRL},
            consumes=(_A_ROOT_CRL,),
        ),
        Step(
            id="addstore-rootcrl",
            command="cert.addstore",
            target=PRIMARY,
            params={"store": "ca", "path": _ROOT_CRL, "kind": "crl"},
        ),
    ]

    # 2) Publish the root cert + CRL into AD (on the DC, where LocalSystem is
    #    directory-privileged).
    if has_dc:
        steps += [
            Step(
                id="root-to-dc",
                command="file.write",
                target=DC,
                params={"path": _ROOT_CRT},
                consumes=(_A_ROOT_CRT,),
            ),
            Step(
                id="dspublish-root",
                command="cert.dspublish",
                target=DC,
                params={"path": _ROOT_CRT, "attribute": "RootCA"},
                retry_delays_s=_AD_RETRY,
            ),
            Step(
                id="rootcrl-to-dc",
                command="file.write",
                target=DC,
                params={"path": _ROOT_CRL},
                consumes=(_A_ROOT_CRL,),
            ),
            Step(
                id="dspublish-rootcrl",
                command="cert.dspublish",
                target=DC,
                params=lambda rt: {"path": _ROOT_CRL, "attribute": root.hostname},
                retry_delays_s=_AD_RETRY,
            ),
        ]

    # 3) Copy the root cert to the web host's served CertEnroll (HTTP CDP/AIA).
    if has_web:
        steps += [
            Step(
                id="root-to-web",
                command="file.write",
                target=WEB,
                params=root_web_crt_path,
                consumes=(_A_ROOT_CRT,),
            ),
            Step(
                id="rootcrl-to-web",
                command="file.write",
                target=WEB,
                params=root_web_crl_path,
                consumes=(_A_ROOT_CRL,),
            ),
        ]

    # 4) Stand up the issuing CA (Enterprise Admin via -Credential) and relay
    #    its CSR out to the offline root, signed cert back.
    steps += [
        Step(
            id="install-issuing",
            command="ca.install",
            target=PRIMARY,
            params=issuing_install_params,
            secret_keys=("password",),
            timeout_s=1800,
        ),
        Step(
            id="read-csr",
            command="file.read",
            target=PRIMARY,
            params={"path": _CSR},
            produces=(_A_ISSUING_CSR,),
        ),
        Step(
            id="csr-to-root",
            command="file.write",
            target=ROOT,
            params={"path": _CSR},
            consumes=(_A_ISSUING_CSR,),
        ),
        Step(
            id="sign-csr",
            command="ca.sign_request",
            target=ROOT,
            params={"csrPath": _CSR, "certPath": _ISSUING_CRT},
        ),
        Step(
            id="read-signed",
            command="file.read",
            target=ROOT,
            params={"path": _ISSUING_CRT},
            produces=(_A_ISSUING_CRT,),
        ),
        Step(
            id="signed-to-ca02",
            command="file.write",
            target=PRIMARY,
            params={"path": _ISSUING_CRT},
            consumes=(_A_ISSUING_CRT,),
        ),
        Step(
            id="install-issuing-cert",
            command="ca.install_cert",
            target=PRIMARY,
            params={"certPath": _ISSUING_CRT},
            verify=_ca_verify_step(),
            verify_predicate=lambda r: r.get("ping_ok") is True,
        ),
    ]

    # 5) Finish CA02 config + publish templates + grant the web host enroll.
    steps += [
        Step(
            id="issuing-settings",
            command="ca.configure_settings",
            target=PRIMARY,
            params=issuing_settings_params,
        ),
        Step(
            id="issuing-cdp-aia",
            command="ca.configure_cdp_aia",
            target=PRIMARY,
            params=issuing_cdp_aia_params,
        ),
        Step(
            id="issuing-crl",
            command="ca.publish_crl",
            target=PRIMARY,
            params=lambda rt: {"certEnrollPath": _ca_publication_dir(rt)},
            retry_delays_s=_CRL_RETRY,
            result_artifacts={
                "certificateFileName": _A_ISSUING_CERT_FILE,
                "baseCrlFileName": _A_ISSUING_CRL_FILE,
                "deltaCrlFileName": _A_ISSUING_DELTA_CRL_FILE,
            },
            result_artifact_defaults=issuing_publication_defaults,
            optional_result_artifacts={
                "certificateContentB64": "issuing_pub_crt",
            },
        ),
    ]
    if has_web:
        steps += [
            Step(
                id="read-issuing-crt",
                command="file.read",
                target=PRIMARY,
                params=issuing_pub_crt_path,
                produces=("issuing_pub_crt",),
                skip_if_artifacts=("issuing_pub_crt",),
            ),
            Step(
                id="issuing-to-web",
                command="file.write",
                target=WEB,
                params=issuing_web_crt_path,
                consumes=("issuing_pub_crt",),
            ),
        ]
    steps.append(
        Step(
            id="publish-templates",
            command="ca.publish_template",
            target=PRIMARY,
            params={"templates": "OCSPResponseSigning,Workstation"},
            retry_delays_s=_TEMPLATE_RETRY,
        )
    )
    if has_web and has_dc:
        steps += [
            Step(
                id="grant-ocsp",
                command="template.grant_access",
                target=DC,
                params=lambda rt: {
                    "template": "OCSPResponseSigning",
                    "computer": ctx.node(WEB).hostname,
                },
                retry_delays_s=_TEMPLATE_RETRY,
            ),
            Step(
                id="grant-health-probe",
                command="template.grant_access",
                target=DC,
                params=lambda rt: {
                    "template": "Workstation",
                    "computer": ctx.node(WEB).hostname,
                },
                retry_delays_s=_TEMPLATE_RETRY,
            ),
        ]
    return steps


def op_sequence(op_kind: str, ctx: RunContext) -> list[Step]:
    """The step list for a non-createVm plan op. domainJoin (slice 10) and
    caConnect (slice 11) are real; webServerCert lands in slice 12, and
    domainLeave keeps the timed simulation stub in ``app.tasks``."""
    if op_kind == "domainJoin":
        return _domain_join_sequence(ctx)
    if op_kind == "domainLeave":
        return _domain_leave_sequence(ctx)
    if op_kind == "caConnect":
        return _ca_connect_sequence(ctx)
    if op_kind == "webServerCert":
        return _web_server_cert_sequence(ctx)
    return []
