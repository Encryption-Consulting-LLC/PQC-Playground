"""Slice-9 wiring: createVm provision sequences + the agentbus frame classifier
(pure pieces, no redis/Mongo)."""

import pytest

from app.core.agentbus import DispatchError, _frame_outcome, _terminal_result
from app.core.sequences.definitions import provision_steps
from app.core.sequences.model import NodeContext, RunContext


def _step(steps, step_id):
    return next(step for step in steps if step.id == step_id)


def test_certificate_authority_root_provisions_install_and_verify():
    steps = provision_steps("certificateAuthority", ca_type="Root")
    # The root tail installs, configures, publishes, and relays (slice 11); the
    # install step's readiness gate is ca.verify.
    install = _step(steps, "ca-install")
    assert install.command == "ca.install"
    assert install.verify is not None
    assert install.verify.command == "ca.verify"


def test_issuing_ca_has_no_first_boot_role_work():
    # An issuing CA can't stand up until the caConnect handshake — its tail is
    # the inherited-reboot settle and nothing else.
    steps = provision_steps("certificateAuthority", ca_type="Issuing")
    assert [step.id for step in steps] == ["settle-reboot"]


def test_templates_without_a_tail_only_settle_their_inherited_reboot():
    # A member server / client / standalone does its role work on join, not on
    # boot — but it still clears the base image's pending reboot first.
    for template in ("webServer", "client", "standalone"):
        assert [step.id for step in provision_steps(template)] == ["settle-reboot"]


def test_ca_install_params_come_from_template_config():
    steps = provision_steps("certificateAuthority", ca_type="Root")
    node = NodeContext(
        node_id="ca",
        vm_name="guest-abc12-ca01",
        hostname="guest-abc12-ca01",
        agent_vm_id="vm-9",
        template_id="certificateAuthority",
        template_config={
            "caType": "Root",
            "commonName": "EC-Root-CA",
            "keyAlgorithm": "RSA",
        },
    )
    ctx = RunContext(nodes={"primary": node})
    params = _step(steps, "ca-install").resolve_params(ctx)
    assert params["commonName"] == "EC-Root-CA"
    assert params["caType"] == "Root"


def test_frame_outcome_classifies_frames():
    assert _frame_outcome({"type": "done", "result": {"ok": 1}}) == (True, {"ok": 1})
    ok, payload = _frame_outcome({"type": "error", "detail": "boom"})
    assert ok is False and payload["detail"] == "boom"
    assert _frame_outcome({"type": "progress", "percent": 50}) is None


def test_terminal_result_returns_done_payload():
    snap = {"status": "done", "last": {"type": "done", "result": {"ip": "10.0.0.5"}}}
    assert _terminal_result(snap) == {"ip": "10.0.0.5"}


def test_terminal_result_reraises_error_snapshot():
    snap = {"status": "error", "last": {"type": "error", "detail": "prior failure"}}
    with pytest.raises(DispatchError):
        _terminal_result(snap)


def test_terminal_result_none_for_nonterminal_snapshot():
    snap = {"status": "running", "last": {"type": "progress", "percent": 10}}
    assert _terminal_result(snap) is None
