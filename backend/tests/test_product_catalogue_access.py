"""The per-account product catalogue.

Capabilities are per-role and answer *whether*; the product grant is
per-account and answers *which*. These pin the three things that make that
distinction hold: an operator is never restricted, a guest is deny-by-default,
and the PKI component templates are outside the catalogue entirely — gating
them would restrict the playground rather than the products.
"""

import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import (  # noqa: E402
    AuthedUser,
    Role,
    allowed_product_templates,
)
from app.core.infrastructure import LINUX_PRODUCT_TEMPLATES  # noqa: E402
from app.routers.admin_users import _validate_products  # noqa: E402
from app.routers.deploy import PlanOp, validate_plan  # noqa: E402

PROJECT = "9e4edb21-6d5f-4d0e-9a4c-0f2b7c8d1e33"


def _guest(products: list[str] | None = None) -> AuthedUser:
    return AuthedUser(
        username="carol", role=Role.GUEST, auth="local", products=products or []
    )


def _operator() -> AuthedUser:
    return AuthedUser(username="op", role=Role.OPERATOR, auth="local")


def _create(template: str, op_id: str = "create-1") -> PlanOp:
    return PlanOp(
        id=op_id,
        kind="createVm",
        target=f"node-{op_id}",
        params={"vmName": op_id, "template": template},
    )


def _validate(ops: list[PlanOp], user: AuthedUser) -> None:
    validate_plan(
        ops,
        user,
        target_configured=True,
        guest_network_configured=True,
        project_id=PROJECT,
    )


def test_an_operator_holds_the_whole_catalogue():
    assert allowed_product_templates(_operator()) == LINUX_PRODUCT_TEMPLATES
    for template in sorted(LINUX_PRODUCT_TEMPLATES):
        _validate([_create(template)], _operator())


def test_a_guest_with_no_grant_gets_no_products():
    # Deny by default: a freshly provisioned guest is limited until an admin
    # widens it, which is the opposite of what an absent field usually means.
    assert allowed_product_templates(_guest()) == frozenset()
    with pytest.raises(HTTPException) as exc:
        _validate([_create("certsecure")], _guest())
    assert exc.value.status_code == 403
    assert "not available to account 'carol'" in str(exc.value.detail)


def test_a_guest_deploys_the_products_it_was_granted_and_no_others():
    granted = _guest(["certsecure"])
    assert allowed_product_templates(granted) == frozenset({"certsecure"})
    _validate([_create("certsecure")], granted)

    with pytest.raises(HTTPException) as exc:
        _validate([_create("codesign")], granted)
    assert exc.value.status_code == 403


def test_the_pki_components_are_outside_the_catalogue():
    # The catalogue restricts products, never the playground: a guest with no
    # product grant at all still stands up a CA and a web server.
    _validate(
        [
            _create("certificateAuthority", "create-ca"),
            _create("webServer", "create-web"),
        ],
        _guest(),
    )


def test_a_grant_is_normalized_and_an_unknown_product_is_refused():
    # Catalogue order, deduplicated — two admins granting the same set write
    # the same document.
    assert _validate_products(["codesign", "certsecure", "certsecure"]) == [
        "certsecure",
        "codesign",
    ]
    assert _validate_products([]) == []
    with pytest.raises(HTTPException) as exc:
        _validate_products(["certsecure", "certsecrue"])
    assert exc.value.status_code == 422
    assert "certsecrue" in str(exc.value.detail)
