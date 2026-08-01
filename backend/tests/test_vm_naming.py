"""The guest VM-name scheme round-trips: what authz builds, vm_naming parses."""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import vm_naming  # noqa: E402
from app.core.authz import AuthedUser, Role, enforce_guest_vm_name  # noqa: E402
from app.core.sequences import context as sequence_context  # noqa: E402


def _guest(username: str) -> AuthedUser:
    return AuthedUser(username=username, role=Role.GUEST, auth="local")


def test_project_segment_is_read_only_when_it_is_exactly_code_shaped():
    # A machine name may itself contain dashes, so the only thing separating
    # "project code + machine" from "machine with a dash" is the code's shape.
    assert vm_naming.parse_guest_vm_name("guest-guest-cc92f3-ca02") == (
        vm_naming.ParsedVmName(user="guest", project_code="cc92f3", machine="ca02")
    )
    assert vm_naming.parse_guest_vm_name("guest-alice-dc01") == (
        vm_naming.ParsedVmName(user="alice", project_code=None, machine="dc01")
    )
    assert vm_naming.parse_guest_vm_name("guest-alice-web-01") == (
        vm_naming.ParsedVmName(user="alice", project_code=None, machine="web-01")
    )
    # Five characters is not a project code, so the whole tail is the machine.
    assert vm_naming.parse_guest_vm_name("guest-alice-abc12-dc") == (
        vm_naming.ParsedVmName(user="alice", project_code=None, machine="abc12-dc")
    )


def test_non_namespaced_and_incomplete_names_do_not_parse():
    assert vm_naming.parse_guest_vm_name("lab-dc01") is None
    assert vm_naming.parse_guest_vm_name("guest-alice") is None
    assert vm_naming.parse_guest_vm_name("guest-alice-") is None


def test_built_names_parse_back_to_the_identity_they_were_built_from():
    user = _guest("alice@corp.com")
    project_id = "467893ab-cdef-4321-abcd-123456789abc"

    with_project = enforce_guest_vm_name("DC-01", user, project_id)
    without_project = enforce_guest_vm_name("DC-01", user)

    assert with_project == "guest-alice-467893-dc-01"
    parsed = vm_naming.parse_guest_vm_name(with_project)
    assert parsed is not None
    assert parsed.user == vm_naming.user_slug(user.username)
    assert parsed.project_code == vm_naming.project_code(project_id)
    assert parsed.machine == "dc-01"

    # The projectless form keeps the user-wide namespace as its prefix.
    assert vm_naming.namespace_prefix(without_project) == vm_naming.guest_namespace(
        user.username
    )


def test_sibling_namespace_prefix_keeps_the_project_segment():
    # The sequence engine's sibling lookup delegates here; these are the exact
    # assertions that scoping a by-role lookup to one environment rests on.
    assert (
        sequence_context._namespace_prefix("guest-guest-cc92f3-ca02")
        == "guest-guest-cc92f3-"
    )
    assert sequence_context._namespace_prefix("guest-alice-dc01") == "guest-alice-"
    assert sequence_context._namespace_prefix("lab-dc01") is None


def test_a_dashed_machine_name_does_not_masquerade_as_a_project():
    # ``guest-alice-web-01`` has no project segment, so its siblings are every
    # VM under ``guest-alice-`` — narrowing to ``guest-alice-web-`` would scope
    # the lookup to a prefix no sibling shares.
    assert sequence_context._namespace_prefix("guest-alice-web-01") == "guest-alice-"
