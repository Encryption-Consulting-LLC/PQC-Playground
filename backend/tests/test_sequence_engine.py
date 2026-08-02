"""Sequence-engine walk logic — reboot waits, verify backoff,
artifact relay, and resume-skip, all with injected effects (no redis/Mongo)."""

import pytest

from app.core.sequences.engine import (
    HealthGateError,
    SequenceCancelled,
    SequenceEngine,
    SequenceError,
    deterministic_step_job_id,
    redact_params,
)
from app.core.sequences.model import NodeContext, RunContext, Step, StepRuntime


def _ctx(**node_overrides):
    node = NodeContext(
        node_id="dc",
        vm_name="guest-abc12-dc01",
        hostname="guest-abc12-dc01",
        agent_vm_id="vm-1",
        template_id="domainController",
        template_config={"domainName": "EC.com"},
        **node_overrides,
    )
    return RunContext(nodes={"primary": node})


class FakeClock:
    def __init__(self):
        self.t = 0

    def now_ms(self):
        return self.t

    def sleep(self, seconds):
        self.t += int(seconds * 1000)


def test_runs_steps_in_order_and_returns_results():
    clock = FakeClock()
    calls = []

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        calls.append(command)
        return {"ok": command}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    steps = [
        Step(id="a", command="ca.install", target="primary"),
        Step(id="b", command="ca.publish_crl", target="primary"),
    ]
    results = engine.run(steps, _ctx())
    assert calls == ["ca.install", "ca.publish_crl"]
    assert results["a"] == {"ok": "ca.install"}


def test_reboot_step_waits_for_reconnect_after_dispatch():
    clock = FakeClock()
    reconnect_args = {}

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        clock.t += 1000  # dispatch takes time
        return {}

    def wait_for_reconnect(vm_id, since_ms, timeout_s):
        reconnect_args["vm_id"] = vm_id
        reconnect_args["since_ms"] = since_ms

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=wait_for_reconnect,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    engine.run(
        [
            Step(
                id="reboot",
                command="system.reboot",
                target="primary",
                expects_disconnect=True,
            )
        ],
        _ctx(),
    )
    # since_ms is captured BEFORE dispatch, so a fast reconnect still counts.
    assert reconnect_args["vm_id"] == "vm-1"
    assert reconnect_args["since_ms"] == 0


def test_verify_retries_until_predicate_passes():
    clock = FakeClock()
    probe_results = iter([{"ready": False}, {"ready": False}, {"ready": True}])

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        if command == "dc.verify":
            return next(probe_results)
        return {}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    step = Step(
        id="install",
        command="dc.install_forest",
        target="primary",
        verify=Step(id="v", command="dc.verify", target="primary"),
        verify_predicate=lambda r: r.get("ready") is True,
        verify_window_s=600,
    )
    engine.run([step], _ctx())  # no raise = the third probe passed


def test_verify_raises_when_window_elapses():
    clock = FakeClock()

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        return {"ready": False}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    step = Step(
        id="install",
        command="dc.install_forest",
        target="primary",
        verify=Step(id="v", command="dc.verify", target="primary"),
        verify_predicate=lambda r: r.get("ready") is True,
        verify_window_s=30,
    )
    with pytest.raises(SequenceError):
        engine.run([step], _ctx())


def test_verify_treats_probe_error_as_not_ready():
    clock = FakeClock()
    attempts = {"n": 0}

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        if command == "dc.verify":
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise SequenceError("ADWS still down")
            return {"ready": True}
        return {}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    step = Step(
        id="install",
        command="dc.install_forest",
        target="primary",
        verify=Step(id="v", command="dc.verify", target="primary"),
        verify_predicate=lambda r: r.get("ready") is True,
    )
    engine.run([step], _ctx())
    assert attempts["n"] == 2


def test_verify_retries_production_dispatch_errors_until_ready():
    clock = FakeClock()
    attempts = {"n": 0}

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        if command == "dc.verify":
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("agent command 'dc.verify' failed: ADWS still down")
            return {"ready": True}
        return {}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    step = Step(
        id="install",
        command="dc.install_forest",
        target="primary",
        verify=Step(id="v", command="dc.verify", target="primary"),
        verify_predicate=lambda r: r.get("ready") is True,
    )

    engine.run([step], _ctx())

    assert attempts["n"] == 3
    assert clock.t == 15_000


def test_failed_step_raises_and_stops_the_sequence():
    clock = FakeClock()
    ran = []

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        ran.append(command)
        if command == "domain.join":
            raise SequenceError("bad credentials")
        return {}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    steps = [
        Step(id="dns", command="dns.set_client", target="primary"),
        Step(id="join", command="domain.join", target="primary"),
        Step(id="after", command="domain.verify", target="primary"),
    ]
    with pytest.raises(SequenceError):
        engine.run(steps, _ctx())
    assert ran == ["dns.set_client", "domain.join"]  # 'after' never ran


def test_completed_steps_skip_on_resume():
    clock = FakeClock()
    ran = []

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        ran.append(command)
        return {}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
        completed={"a"},
    )
    steps = [
        Step(id="a", command="ca.install", target="primary"),
        Step(id="b", command="ca.publish_crl", target="primary"),
    ]
    engine.run(steps, _ctx())
    assert ran == ["ca.publish_crl"]  # 'a' was already done


def test_completed_results_feed_resumable_aggregate():
    clock = FakeClock()
    aggregate_inputs = {}

    def aggregate(_rt, results):
        aggregate_inputs.update(results)
        return {"healthy": results["probe"]["ready"]}

    engine = SequenceEngine(
        dispatch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no dispatch")),
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
        completed={"probe"},
        resumed_results={"probe": {"ready": True}},
    )
    results = engine.run(
        [
            Step(id="probe", command="dc.verify", target="primary"),
            Step(
                id="gate",
                command="lab.verify",
                target="primary",
                aggregate=aggregate,
            ),
        ],
        _ctx(),
    )

    assert aggregate_inputs == {"probe": {"ready": True}}
    assert results["gate"] == {"healthy": True}


def test_unhealthy_aggregate_fails_sequence_with_reasons():
    clock = FakeClock()
    engine = SequenceEngine(
        dispatch=lambda *a, **k: {},
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    gate = Step(
        id="gate",
        command="lab.verify",
        target="primary",
        aggregate=lambda _rt, _results: {
            "healthy": False,
            "failures": ["OCSP response was not verified"],
        },
    )

    with pytest.raises(
        HealthGateError, match="OCSP response was not verified"
    ) as error:
        engine.run([gate], _ctx())

    assert error.value.step_id == "gate"
    assert error.value.health["healthy"] is False
    assert error.value.results["gate"] == error.value.health


def test_produces_and_consumes_relay_artifacts():
    clock = FakeClock()
    seen_params = {}

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        if command == "file.read":
            return {"contentB64": "Q1NSLWJ5dGVz"}
        if command == "ca.sign_request":
            seen_params.update(params)
            return {}
        return {}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    steps = [
        Step(id="read", command="file.read", target="primary", produces=("csr",)),
        Step(id="sign", command="ca.sign_request", target="primary", consumes=("csr",)),
    ]
    ctx = _ctx()
    engine.run(steps, ctx)
    assert ctx.artifacts["csr"] == "Q1NSLWJ5dGVz"
    assert seen_params["contentB64"] == "Q1NSLWJ5dGVz"


def test_persists_required_result_fields_as_cross_operation_artifacts():
    clock = FakeClock()
    ctx = _ctx()
    engine = SequenceEngine(
        dispatch=lambda *a, **k: {
            "certificateFileName": "CA02_Issuing CA.crt",
            "baseCrlFileName": "Issuing CA.crl",
        },
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    engine.run(
        [
            Step(
                id="publish",
                command="ca.publish_crl",
                target="primary",
                result_artifacts={
                    "certificateFileName": "issuing_cert_filename",
                    "baseCrlFileName": "issuing_crl_filename",
                },
            )
        ],
        ctx,
    )

    assert ctx.artifacts["issuing_cert_filename"] == "CA02_Issuing CA.crt"
    assert ctx.artifacts["issuing_crl_filename"] == "Issuing CA.crl"


def test_missing_required_result_artifact_fails_the_step():
    clock = FakeClock()
    engine = SequenceEngine(
        dispatch=lambda *a, **k: {},
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    with pytest.raises(SequenceError, match="certificateFileName"):
        engine.run(
            [
                Step(
                    id="publish",
                    command="ca.publish_crl",
                    target="primary",
                    result_artifacts={"certificateFileName": "cert_filename"},
                )
            ],
            _ctx(),
        )


def test_missing_result_artifact_uses_explicit_legacy_agent_default():
    clock = FakeClock()
    ctx = _ctx()
    engine = SequenceEngine(
        dispatch=lambda *a, **k: {"published": True},
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    engine.run(
        [
            Step(
                id="publish",
                command="ca.publish_crl",
                target="primary",
                result_artifacts={"certificateFileName": "cert_filename"},
                result_artifact_defaults={"certificateFileName": "dc_Example CA.crt"},
            )
        ],
        ctx,
    )

    assert ctx.artifacts["cert_filename"] == "dc_Example CA.crt"


def test_optional_result_artifact_is_persisted_when_new_agent_reports_it():
    clock = FakeClock()
    ctx = _ctx()
    engine = SequenceEngine(
        dispatch=lambda *a, **k: {"certificateContentB64": "Y2VydA=="},
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    engine.run(
        [
            Step(
                id="publish",
                command="ca.publish_crl",
                target="primary",
                optional_result_artifacts={"certificateContentB64": "certificate"},
            )
        ],
        ctx,
    )

    assert ctx.artifacts["certificate"] == "Y2VydA=="


def test_artifact_preproduced_by_publish_skips_legacy_file_read():
    clock = FakeClock()
    ctx = _ctx()
    ctx.artifacts["certificate"] = "Y2VydA=="
    completed = []
    engine = SequenceEngine(
        dispatch=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("file.read must be skipped")
        ),
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
        on_step_done=lambda step_id, result: completed.append((step_id, result)),
    )

    result = engine.run(
        [
            Step(
                id="read",
                command="file.read",
                target="primary",
                skip_if_artifacts=("certificate",),
            )
        ],
        ctx,
    )

    assert result["read"]["skipped"] is True
    assert completed[0][0] == "read"


def test_missing_file_result_artifact_fails_at_the_producer():
    clock = FakeClock()
    engine = SequenceEngine(
        dispatch=lambda *a, **k: {},
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    with pytest.raises(SequenceError, match="contentB64"):
        engine.run(
            [
                Step(
                    id="read-cert",
                    command="file.read",
                    target="primary",
                    produces=("certificate",),
                )
            ],
            _ctx(),
        )


def test_missing_consumed_artifact_fails_before_dispatch():
    clock = FakeClock()
    dispatched = []
    engine = SequenceEngine(
        dispatch=lambda *a, **k: dispatched.append(a),
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    with pytest.raises(SequenceError, match="unavailable artifact 'certificate'"):
        engine.run(
            [
                Step(
                    id="write-cert",
                    command="file.write",
                    target="primary",
                    consumes=("certificate",),
                )
            ],
            _ctx(),
        )

    assert dispatched == []


def test_param_resolver_sees_context():
    clock = FakeClock()
    seen = {}

    def resolver(rt: StepRuntime):
        return {
            "domainName": rt.node.template_config["domainName"],
            "host": rt.node.hostname,
        }

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        seen.update(params)
        return {}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    engine.run(
        [Step(id="s", command="dc.install_forest", target="primary", params=resolver)],
        _ctx(),
    )
    assert seen == {"domainName": "EC.com", "host": "guest-abc12-dc01"}


def test_deterministic_step_job_id_is_stable():
    assert deterministic_step_job_id("job1", "op2", "step3") == "job1-op2-step3"


def test_redact_params_masks_secrets():
    out = redact_params({"username": "admin", "password": "hunter2"}, ["password"])
    assert out == {"username": "admin", "password": "***"}


def test_transient_dispatch_failures_use_bounded_retry_schedule():
    clock = FakeClock()
    keys = []

    def dispatch(job_key, *_args, **_kwargs):
        keys.append(job_key)
        if len(keys) < 3:
            raise SequenceError("service is still replicating")
        return {"ready": True}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    result = engine.run(
        [
            Step(
                id="publish",
                command="ca.publish_template",
                target="primary",
                retry_delays_s=(10, 20),
            )
        ],
        _ctx(),
    )

    assert result["publish"] == {"ready": True}
    assert keys == ["publish", "publish.retry.1", "publish.retry.2"]
    assert clock.t == 30_000


def test_retry_policy_reraises_after_final_attempt():
    clock = FakeClock()
    engine = SequenceEngine(
        dispatch=lambda *a, **k: (_ for _ in ()).throw(SequenceError("still down")),
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    with pytest.raises(SequenceError, match="still down"):
        engine.run(
            [
                Step(
                    id="dns",
                    command="dns.verify",
                    target="primary",
                    retry_delays_s=(5,),
                )
            ],
            _ctx(),
        )
    assert clock.t == 5_000


def test_a_timed_out_step_is_never_redispatched():
    """Giving up on the wait doesn't stop the command on the guest, so a retry
    would race a second Install-WindowsFeature against the first. The retry
    schedule covers transient *failures*, not timeouts."""
    clock = FakeClock()
    keys = []

    # The engine stays transport-agnostic: it reads the adapter's `timed_out`
    # marker, which agentbus.DispatchTimeout carries.
    class Timeout(SequenceError):
        timed_out = True

    def dispatch(job_key, *_args, **_kwargs):
        keys.append(job_key)
        raise Timeout("agent command 'iis.setup_certenroll' timed out after 1800s")

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    with pytest.raises(SequenceError, match="timed out"):
        engine.run(
            [
                Step(
                    id="iis-web",
                    command="iis.setup_certenroll",
                    target="primary",
                    retry_delays_s=(10, 20),
                )
            ],
            _ctx(),
        )
    assert keys == ["iis-web"]
    assert clock.t == 0


def test_cancellation_stops_between_steps_without_interrupting_dispatch():
    clock = FakeClock()
    commands = []

    def dispatch(_key, _vm, command, *_args, **_kwargs):
        commands.append(command)
        return {"ok": True}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
        should_stop=lambda: len(commands) == 1,
    )

    with pytest.raises(SequenceCancelled, match="before step 'second'"):
        engine.run(
            [
                Step(id="first", command="dns.verify", target="primary"),
                Step(id="second", command="ca.verify", target="primary"),
            ],
            _ctx(),
        )
    assert commands == ["dns.verify"]


# The real detail from a failed forest promotion: Install-WindowsFeature left a
# restart pending and Install-ADDSForest's prereq check refused in the same
# session (FullyQualifiedErrorId Test.VerifyDcPromoCore.DCPromo.General.15).
_PREREQ_DETAIL = (
    "agent command 'dc.install_forest' failed: powershell execution failed: "
    "script exited with code 1: Install-ADDSForest : Verification of "
    "prerequisites for Domain Controller promotion failed. Role change is in "
    "progress or this computer needs to be restarted."
)


def _forest_step(**overrides):
    return Step(
        id="install-forest",
        command="dc.install_forest",
        target="primary",
        secret_keys=("safeModePassword",),
        timeout_s=1800,
        reboot_recovery_signatures=("dcpromo.general.15", "needs to be restarted"),
        **overrides,
    )


def test_pending_reboot_failure_reboots_and_redispatches_once():
    clock = FakeClock()
    calls = []
    reconnects = []

    def dispatch(job_key, vm_id, command, params, **kwargs):
        calls.append((job_key, command, params, kwargs["expect_disconnect"]))
        if len(calls) == 1:
            raise SequenceError(_PREREQ_DETAIL)
        return {"promoted": command}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *args: reconnects.append(args),
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )
    result = engine.run([_forest_step()], _ctx())

    assert result["install-forest"] == {"promoted": "dc.install_forest"}
    assert [(key, command) for key, command, _p, _d in calls] == [
        ("install-forest", "dc.install_forest"),
        ("install-forest.rebootrecover", "system.reboot"),
        ("install-forest.postreboot", "dc.install_forest"),
    ]
    # The reboot is a real disconnect-expecting restart, waited out on the
    # recovery budget before the command is tried again.
    assert calls[1][2] == {"delaySeconds": "5"}
    assert calls[1][3] is True
    assert reconnects == [("vm-1", 0, 1200)]


def test_unrelated_failure_does_not_trigger_a_recovery_reboot():
    clock = FakeClock()
    commands = []

    def dispatch(_key, _vm, command, *_args, **_kwargs):
        commands.append(command)
        raise SequenceError("safe mode password does not meet complexity rules")

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    with pytest.raises(SequenceError, match="complexity"):
        engine.run([_forest_step()], _ctx())
    assert commands == ["dc.install_forest"]


def test_reboot_recovery_is_one_shot():
    clock = FakeClock()
    commands = []

    def dispatch(_key, _vm, command, *_args, **_kwargs):
        commands.append(command)
        if command == "dc.install_forest":
            raise SequenceError(_PREREQ_DETAIL)
        return {"ok": True}

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=lambda *a, **k: None,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    with pytest.raises(SequenceError, match="needs to be restarted"):
        engine.run([_forest_step()], _ctx())
    assert commands == ["dc.install_forest", "system.reboot", "dc.install_forest"]


def test_failed_recovery_reboot_surfaces_the_original_failure():
    clock = FakeClock()

    def dispatch(_key, _vm, command, *_args, **_kwargs):
        if command == "dc.install_forest":
            raise SequenceError(_PREREQ_DETAIL)
        return {"ok": True}

    def wait_for_reconnect(*_args):
        raise SequenceError("agent never came back")

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=wait_for_reconnect,
        sleep=clock.sleep,
        now_ms=clock.now_ms,
    )

    # The blocker the operator needs to see is the prereq refusal, not the
    # recovery attempt that also failed.
    with pytest.raises(SequenceError, match="prerequisites") as excinfo:
        engine.run([_forest_step()], _ctx())
    assert "never came back" in str(excinfo.value.__cause__)
