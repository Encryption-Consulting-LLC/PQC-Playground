"""Structural invariants of the role allowlist.

``ROLE_CAPABILITIES`` writes the admin-only set out twice — granted to admin,
subtracted from operator. These tests are what catch the next capability that
gets added to one list and forgotten in the other.
"""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import ROLE_CAPABILITIES, Capability, Role  # noqa: E402


def test_admin_and_operator_are_disjoint():
    # A capability granted to admin and forgotten in operator's subtraction
    # shows up here, not as a silent cross-role grant in production.
    assert ROLE_CAPABILITIES[Role.ADMIN] & ROLE_CAPABILITIES[Role.OPERATOR] == set()


def test_admin_and_operator_cover_every_capability():
    assert (ROLE_CAPABILITIES[Role.ADMIN] | ROLE_CAPABILITIES[Role.OPERATOR]) == set(
        Capability
    )


def test_guest_is_a_subset_of_operator():
    assert ROLE_CAPABILITIES[Role.GUEST] <= ROLE_CAPABILITIES[Role.OPERATOR]


def test_cross_user_teardown_is_admin_only_and_distinct_from_vm_delete():
    # Admins tear down other people's labs; they still can't delete a VM
    # through the operator/guest route, whose own-namespace check is the only
    # thing standing between one guest and another's environment.
    assert Capability.VM_TEARDOWN_ADMIN in ROLE_CAPABILITIES[Role.ADMIN]
    assert Capability.VM_DELETE not in ROLE_CAPABILITIES[Role.ADMIN]
    assert Capability.VM_TEARDOWN_ADMIN not in ROLE_CAPABILITIES[Role.OPERATOR]
    assert Capability.VM_TEARDOWN_ADMIN not in ROLE_CAPABILITIES[Role.GUEST]


def test_lab_join_is_a_canvas_capability_and_issuing_is_admin_only():
    # The two halves of a join code: guests and operators redeem one, only an
    # admin hands one out. Reversing either is how a lab meant for one cohort
    # becomes self-serve.
    assert Capability.LAB_JOIN in ROLE_CAPABILITIES[Role.GUEST]
    assert Capability.LAB_JOIN in ROLE_CAPABILITIES[Role.OPERATOR]
    assert Capability.LAB_ADMIN in ROLE_CAPABILITIES[Role.ADMIN]
    assert Capability.LAB_ADMIN not in ROLE_CAPABILITIES[Role.OPERATOR]
    assert Capability.LAB_ADMIN not in ROLE_CAPABILITIES[Role.GUEST]
