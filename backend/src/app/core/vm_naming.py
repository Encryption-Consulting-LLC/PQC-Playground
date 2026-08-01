"""The guest VM-name scheme, in one place.

Every VM a guest deploys is named ``guest-<user>-<project>-<machine>`` (or
``guest-<user>-<machine>`` for a projectless direct clone). Three consumers
need that scheme: ``core/authz`` *builds* names from an authenticated identity,
``core/sequences/context`` *parses* one back to the prefix a VM's siblings
share, and the admin teardown console groups registry rows by the owner and
project a name carries. Construction and parsing must agree exactly — a
namespace prefix that doesn't round-trip silently widens or narrows an
ownership boundary — so both live here.

Stdlib only, and deliberately free of ``fastapi`` and ``core.identity``: the
Celery worker and the backfill CLI import this without pulling in the auth
stack. ``core/authz`` keeps its ``AuthedUser``-aware wrappers on top.

Parsing the project segment is the one subtle rule. A project code is exactly
``_PROJECT_CODE_LEN`` alphanumerics (``project_code`` below produces nothing
else), and a machine segment may itself contain dashes, so segment 2 counts as
a project code only when it matches that shape exactly::

    guest-guest-cc92f3-ca02   → project cc92f3, machine ca02
    guest-alice-dc01          → no project,     machine dc01
    guest-alice-web-01        → no project,     machine web-01
    guest-alice-abc12-dc      → no project,     machine abc12-dc  (5 chars)

The scheme is lossy in one direction on purpose: ``project_code`` reduces a
UUID to six characters, so a name yields a project *code*, never the project
id it came from. Anything that needs the real id must read a persisted field.
"""

import re
from dataclasses import dataclass

#: Per-segment caps. The worst case (12 + 6 + 20 + separators + the ``guest-``
#: literal ≈ 46 chars) sits well under ESXi's ~80-char VM-name ceiling. The
#: guest OS *hostname* is derived separately (``core/firstboot.hostname_for``,
#: 15-char NetBIOS), so these caps never have to satisfy the NetBIOS limit —
#: that function keeps its own tail-extraction for the same reason.
GUEST_USER_MAX = 12
GUEST_MACHINE_MAX = 20
PROJECT_CODE_LEN = 6

#: The literal every namespaced name starts with.
GUEST_PREFIX = "guest"

_UNSAFE_NAME_CHARS = re.compile(r"[^a-z0-9-]")
_PROJECT_CODE_RE = re.compile(rf"^[a-z0-9]{{{PROJECT_CODE_LEN}}}$")


@dataclass(frozen=True)
class ParsedVmName:
    """A namespaced VM name split back into its parts.

    ``project_code`` is None for a projectless direct clone (and for any name
    whose second segment isn't code-shaped — see the module docstring).
    """

    user: str
    project_code: str | None
    machine: str


def name_slug(value: str, maxlen: int) -> str:
    """Lowercase, coerce to a safe ``[a-z0-9-]`` slug, collapse runs of and
    strip leading/trailing separators, then cap to ``maxlen`` (re-stripping a
    trailing ``-`` the cut may expose)."""
    slug = re.sub(r"-{2,}", "-", _UNSAFE_NAME_CHARS.sub("-", value.lower())).strip("-")
    return slug[:maxlen].strip("-")


def user_slug(username: str) -> str:
    """Readable per-identity slug: the local part of an email-style username
    (so ``a@corp.com`` → ``a``, not ``a-corp-com``), slugified and capped."""
    local = username.split("@", 1)[0]
    return name_slug(local, GUEST_USER_MAX) or "anon"


def guest_namespace(username: str) -> str:
    """Stable per-identity VM-name prefix, derived from the authenticated
    username (never trusted from the client). Every guest VM name starts with
    this, so it doubles as the ownership boundary."""
    return f"{GUEST_PREFIX}-{user_slug(username)}-"


def project_code(project_id: str) -> str:
    """Short opaque project segment: the leading alphanumerics of the project
    id (a client-generated UUID hex / slug), lowercased and capped.

    Irreversible — six characters of a UUID identify a project *within* a
    user's VMs, not globally, and can never be turned back into the id.
    """
    return re.sub(r"[^a-z0-9]", "", project_id.lower())[:PROJECT_CODE_LEN]


def parse_guest_vm_name(vm_name: str) -> ParsedVmName | None:
    """Split a namespaced VM name, or None for a non-namespaced (operator) one.

    None also covers a name that starts with the literal but carries no machine
    segment (``guest-alice``) — there is no identity to attribute it to.
    """
    parts = vm_name.split("-")
    if parts[0] != GUEST_PREFIX or len(parts) < 3 or not parts[1]:
        return None
    if len(parts) >= 4 and _PROJECT_CODE_RE.match(parts[2]):
        code, machine = parts[2], "-".join(parts[3:])
    else:
        code, machine = None, "-".join(parts[2:])
    if not machine:
        return None
    return ParsedVmName(user=parts[1], project_code=code, machine=machine)


def namespace_prefix(vm_name: str) -> str | None:
    """The prefix a namespaced VM's siblings share, or None for a
    non-namespaced (operator) name.

    Keeping the project segment matters: ``guest-<user>-`` alone spans *every*
    project that guest ever deployed, wide enough for a sibling lookup to land
    on a torn-down environment. A projectless direct clone has no plan siblings
    to find, so it keeps the user-wide prefix.
    """
    parsed = parse_guest_vm_name(vm_name)
    if parsed is None:
        return None
    if parsed.project_code is None:
        return f"{GUEST_PREFIX}-{parsed.user}-"
    return f"{GUEST_PREFIX}-{parsed.user}-{parsed.project_code}-"
