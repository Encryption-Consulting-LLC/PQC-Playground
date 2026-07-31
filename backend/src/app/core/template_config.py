"""Server-side schema for per-template Inspector config.

The frontend spreads a node's ``config`` map flat into a ``createVm`` op's
``params`` (``store/staging.ts::buildOpPayload``). Those values (CA algorithm,
key length, common name, …) are what the backend later dispatches to the
executor as command params once the agent phones home. This module is the
**authoritative** validator for them — the client map is never trusted:

* ``validate_template_config`` rejects (422) any unknown config key (the
  key-injection gate), any bad value for a known key, and any ``required`` field
  the caller omitted or left blank;
* ``extract_template_config`` returns the provisioning-relevant subset with
  defaults filled, ready to persist on the VM registry and later dispatch.

Hand-mirrors ``frontend/src/constants/templates.ts`` (the same documented-mirror
convention as ``core.firstboot.TEMPLATE_IDS``): when a template's ``configFields``
change there, update the matching entry here.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

#: Keys that always ride in a createVm op's params but are not template config.
RESERVED_PARAM_KEYS = frozenset({"vmName", "template", "isoId"})

#: The domain controller's operator-set credential. Dispatched as
#: ``Install-ADDSForest``'s DSRM/``safeModePassword`` *and* rendered into the DC's
#: firstboot ``50-password.ps1`` (``core.firstboot``) — the latter is what makes it
#: the domain Administrator password once the forest is promoted, and therefore the
#: credential ``domain.join`` and the issuing-CA install sign in with.
DOMAIN_ADMIN_PASSWORD_KEY = "domainAdminPassword"

#: AD-complexity policy for operator-set passwords (the DC's
#: ``domainAdminPassword``). Mirrors ``frontend/src/lib/passwordPolicy.ts`` —
#: this is the authoritative gate; the frontend checklist is a convenience.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MIN_CLASSES = 3


def password_policy_errors(value: str, vm_name: str = "") -> list[str]:
    """Every AD-complexity rule *value* fails, empty when it passes.

    ≥12 chars; ≥3 of {lower, upper, digit, symbol}; must not contain
    "administrator" or the machine name (the first two an attacker guesses).
    """
    errors: list[str] = []
    if len(value) < PASSWORD_MIN_LENGTH:
        errors.append(f"must be at least {PASSWORD_MIN_LENGTH} characters")
    classes = sum(
        bool(re.search(pattern, value))
        for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )
    if classes < PASSWORD_MIN_CLASSES:
        errors.append(
            f"must include at least {PASSWORD_MIN_CLASSES} of: lowercase, "
            "uppercase, digit, symbol"
        )
    lowered = value.lower()
    name = vm_name.strip().lower()
    if "administrator" in lowered or (len(name) >= 3 and name in lowered):
        errors.append("must not contain 'Administrator' or the machine name")
    return errors


# Free-text value shapes. Deliberately strict: these values are later
# interpolated by executor PowerShell (via param() blocks), so quotes,
# semicolons, backticks and `$` are excluded even though the param-block layer
# already neutralizes them — defence in depth.
_DNS = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_NETBIOS = re.compile(r"^[A-Za-z0-9-]{1,15}$")
_COMMON_NAME = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
_CERT_PATH = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9 ._\\-]{1,120}$")
_HTTP_URL = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{1,200}$")


def _reverse_zone(value: str) -> bool:
    """Accept an optional IPv4 reverse-zone name such as 100.168.192.in-addr.arpa."""

    if not value:
        return True
    normalized = value.lower().rstrip(".")
    suffix = ".in-addr.arpa"
    if not normalized.endswith(suffix):
        return False
    labels = normalized[: -len(suffix)].split(".")
    return 1 <= len(labels) <= 3 and all(
        label.isdigit() and 0 <= int(label) <= 255 for label in labels
    )


def _one_of(*options: str) -> Callable[[str], bool]:
    allowed = frozenset(options)
    return lambda v: v in allowed


def _matches(pattern: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda v: bool(pattern.match(v))


def _int_between(lo: int, hi: int) -> Callable[[str], bool]:
    def check(v: str) -> bool:
        try:
            return lo <= int(v) <= hi
        except ValueError:
            return False

    return check


@dataclass(frozen=True)
class FieldSpec:
    validate: Callable[[str], bool]
    default: str
    #: Whether this field is dispatched to the executor. Display-only fields
    #: (e.g. the CA ``keyLengthFixed`` label) are accepted but never provisioned.
    provision: bool = True
    #: A secret (password): validated against the AD-complexity policy rather
    #: than ``validate``, encrypted at rest (``encrypt_config_secrets``), and
    #: never logged, returned by an API, or echoed into an op label.
    secret: bool = False
    #: Must be present *and* non-blank in the submitted params.
    #: ``validate_template_config`` otherwise only sees the keys a caller sent, so
    #: an omitted field is invisible to it and ``extract_template_config`` quietly
    #: fills the default — for the DC's password that meant a forest promoted with
    #: an empty DSRM password and an unjoinable domain, surfacing four sequence
    #: steps deep instead of at the 422 gate.
    required: bool = False


# Mirror of the CA/DC/webServer ``configFields`` in templates.ts. Templates with
# no configurable fields (client, standalone) map to an empty schema, so any
# stray config key on them is rejected.
TEMPLATE_CONFIG_FIELDS: dict[str, dict[str, FieldSpec]] = {
    "domainController": {
        "domainName": FieldSpec(_matches(_DNS), "encon.pki"),
        "netbiosName": FieldSpec(_matches(_NETBIOS), "ENCON"),
        "forestLevel": FieldSpec(
            _one_of(
                "Windows Server 2016",
                "Windows Server 2019",
                "Windows Server 2022",
                "Windows Server 2025",
            ),
            "Windows Server 2016",
        ),
        # Optional advanced override. When blank, no reverse zone/PTR resources
        # are planned; operators can use an existing external reverse-DNS setup.
        "reverseZone": FieldSpec(_reverse_zone, ""),
        # Operator-set; injected as a secret command param into domain joins and
        # the issuing-CA install, and rendered into this DC's firstboot password
        # script so that credential actually exists. ``validate`` is a
        # placeholder — secrets go through ``password_policy_errors`` in
        # ``validate_template_config``.
        DOMAIN_ADMIN_PASSWORD_KEY: FieldSpec(
            lambda _v: True, "", secret=True, required=True
        ),
    },
    "certificateAuthority": {
        "caType": FieldSpec(_one_of("Root", "Issuing"), "Root"),
        "commonName": FieldSpec(_matches(_COMMON_NAME), "EC-Root-CA"),
        "certEnrollPath": FieldSpec(
            _matches(_CERT_PATH),
            "C:\\Windows\\System32\\CertSrv\\CertEnroll",
        ),
        "keyAlgorithm": FieldSpec(_one_of("RSA", "ECDSA", "ML-DSA-87"), "ML-DSA-87"),
        "keyLength": FieldSpec(_one_of("2048", "4096"), "2048"),
        # Display-only label shown for ML-DSA-87 (fixed key size) — accepted so
        # the frontend can send it, but never dispatched.
        "keyLengthFixed": FieldSpec(
            lambda v: len(v) <= 32, "2,592 bytes", provision=False
        ),
        "hashAlgorithm": FieldSpec(_one_of("SHA256", "SHA384", "SHA512"), "SHA256"),
        "validityYears": FieldSpec(_int_between(1, 50), "20"),
        # Issuing-CA CPS statement URL (hidden for Root in the UI); the Rust
        # ca.install drops it when caType=Root.
        "cpsUrl": FieldSpec(_matches(_HTTP_URL), "http://pki.encon.pki/cps.txt"),
    },
    "webServer": {
        "certEnrollPath": FieldSpec(_matches(_CERT_PATH), "C:\\CertEnroll"),
        "enableOcsp": FieldSpec(_one_of("Enabled", "Disabled"), "Enabled"),
        "ocspRefreshMinutes": FieldSpec(_int_between(1, 1440), "15"),
    },
    "client": {},
    "standalone": {},
    "certsecure": {},
    "cbom": {},
    "codesign": {},
}


def validate_template_config(template: str, params: Mapping[str, str]) -> None:
    """Raise ``ValueError`` on a missing required field, any unknown config key,
    or a bad known value.

    ``params`` is the whole createVm param map; reserved keys (vmName/template/
    isoId) are skipped. An unknown template is treated as having no config
    fields, so any config key on it is rejected. Secret fields are validated
    against the AD-complexity policy (with the VM name in scope), not their
    placeholder ``validate``.

    Required fields are checked first, over the *schema* rather than the params,
    so an omitted key is caught as crisply as a blank one.
    """
    schema = TEMPLATE_CONFIG_FIELDS.get(template, {})
    vm_name = params.get("vmName", "")
    for key, spec in schema.items():
        if spec.required and not params.get(key, "").strip():
            raise ValueError(
                f"config field '{key}' is required for template '{template}'"
            )
    for key, value in params.items():
        if key in RESERVED_PARAM_KEYS:
            continue
        spec = schema.get(key)
        if spec is None:
            raise ValueError(f"unknown config field '{key}' for template '{template}'")
        if spec.secret:
            errors = password_policy_errors(value, vm_name)
            if errors:
                raise ValueError(f"invalid value for config field '{key}': {errors[0]}")
        elif not spec.validate(value):
            raise ValueError(f"invalid value for config field '{key}'")


def secret_config_keys(template: str) -> frozenset[str]:
    """The provisionable secret keys for ``template`` (e.g. the DC's password)."""
    schema = TEMPLATE_CONFIG_FIELDS.get(template, {})
    return frozenset(
        key for key, spec in schema.items() if spec.secret and spec.provision
    )


def extract_template_config(template: str, params: Mapping[str, str]) -> dict[str, str]:
    """The provisioning-relevant config for ``template``, defaults filled.

    Returns only provisionable fields (drops display-only ones and reserved
    keys), **plaintext** — secrets included. This is the pre-persist /
    pre-dispatch form; encrypt secrets with ``encrypt_config_secrets`` before
    writing to Mongo. Assumes ``validate_template_config`` has already passed.
    """
    schema = TEMPLATE_CONFIG_FIELDS.get(template, {})
    config = {
        key: params.get(key, spec.default)
        for key, spec in schema.items()
        if spec.provision
    }
    # ML-DSA-87 has no *caller-selectable* RSA key length and no external hash:
    # the parameter set fixes both (20736-bit public key, NoHash). Those UI
    # fields are display-hidden for ML-DSA but the defaults above still fill the
    # stale RSA-shaped "2048"/"SHA256" — meaningless for a PQC CA. Drop them so
    # the persisted registry config doesn't misrepresent the algorithm. This is
    # cosmetic for dispatch: the agent's `ca.install` derives the real ML-DSA
    # provider string + KeyLength(20736)/HashAlgorithm(NoHash) itself from
    # keyAlgorithm and ignores whatever crypto params the backend sends for it.
    if template == "certificateAuthority" and config.get("keyAlgorithm") == "ML-DSA-87":
        config.pop("keyLength", None)
        config.pop("hashAlgorithm", None)
    return config


def encrypt_config_secrets(template: str, config: Mapping[str, str]) -> dict:
    """Return ``config`` with every secret field's value replaced by an
    AES-GCM blob (``{keyId, nonce, ciphertext}``) — the at-rest form written to
    the VM registry. Non-secret fields are copied through unchanged. An empty
    secret (operator left it blank) is dropped, not encrypted.

    Its inverse is ``decrypt_config_secrets``, used at dispatch time.
    """
    from app.core.secrets import encrypt_secret

    secrets = secret_config_keys(template)
    out: dict = {}
    for key, value in config.items():
        if key in secrets:
            if value:
                out[key] = encrypt_secret(value)
        else:
            out[key] = value
    return out


def decrypt_config_secrets(template: str, config: Mapping) -> dict[str, str]:
    """Inverse of ``encrypt_config_secrets``: decrypt every secret blob back to
    plaintext, ready to inject as a command param. A missing secret stays
    absent (the operator left it blank)."""
    from app.core.secrets import decrypt_secret

    secrets = secret_config_keys(template)
    out: dict[str, str] = {}
    for key, value in config.items():
        if key in secrets and isinstance(value, Mapping):
            out[key] = decrypt_secret(dict(value))
        else:
            out[key] = value
    return out
