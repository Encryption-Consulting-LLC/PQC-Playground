"""Step duration metrics + elapsed-progress feedback (issue 4 of the boot-gate
fix): the sequence worker must time every dispatched step into ``step_metrics``
and thread the dispatch poll's tick heartbeat outward, and the plan runner's
progress callbacks must turn that heartbeat into elapsed text (plus a
duration-estimated percent once a command has history)."""

import os

import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import agentbus
from app.core.sequences.model import NodeContext, RunContext, Step
from app.core.sequences.worker import _persist_step, load_step_medians, run_op_sequence
from app.tasks import _fmt_duration, _sequence_progress, _step_median_seconds


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction):
        self.docs.sort(key=lambda d: d.get(key, 0), reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __iter__(self):
        return iter(self.docs)


class FakeCollection:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def find_one(self, *_a, **_k):
        return None

    def find(self, flt=None, _proj=None):
        flt = flt or {}
        return FakeCursor(
            d for d in self.docs if all(d.get(k) == v for k, v in flt.items())
        )

    def update_one(self, *_a, **_k):
        self.last_update = _a[1]


class FakeDb(dict):
    def __getitem__(self, name):
        if name not in self:
            super().__setitem__(name, FakeCollection())
        return super().__getitem__(name)


def _ctx():
    return RunContext(
        nodes={
            "primary": NodeContext(
                node_id="dc",
                vm_name="guest-ab123-dc01",
                hostname="guest-ab123-dc01",
                agent_vm_id="vm-1",
            )
        }
    )


# --------------------------------------------------------------------------- #
# run_op_sequence: metrics insert + tick threading                            #
# --------------------------------------------------------------------------- #
def test_run_op_sequence_records_metrics_and_threads_ticks(monkeypatch):
    db = FakeDb()
    ticks = []

    def fake_dispatch(
        vm_id,
        command,
        params,
        *,
        job_id,
        role,
        timeout_s,
        secret_keys=(),
        expect_disconnect=False,
        on_progress=None,
        on_tick=None,
        client=None,
    ):
        # Two frameless poll ticks, then the terminal result.
        if on_tick is not None:
            on_tick(1.0)
            on_tick(2.0)
        return {"ok": True}

    monkeypatch.setattr(agentbus, "dispatch_and_wait", fake_dispatch)

    steps = [Step(id="install-forest", command="dc.install_forest", target="primary")]
    run_op_sequence(
        db,
        steps,
        _ctx(),
        plan_job_id="job-1",
        op_id="op-1",
        role="guest",
        on_step_tick=lambda step_id, elapsed: ticks.append((step_id, elapsed)),
    )

    assert ticks == [("install-forest", 1.0), ("install-forest", 2.0)]
    metrics = db["step_metrics"].docs
    assert len(metrics) == 1
    m = metrics[0]
    assert m["command"] == "dc.install_forest"
    assert m["stepId"] == "install-forest"
    assert m["vmId"] == "vm-1"
    assert isinstance(m["durationMs"], int) and m["durationMs"] >= 0
    assert isinstance(m["at"], int)


def test_a_retried_step_reports_progress_against_its_own_row(monkeypatch):
    """The regression: a retried / post-reboot dispatch forwarded its *job key* as
    the step id, so the row the panel shows stayed grey (no state ever arrived for
    it) while `iis-share.retry.1` accumulated the real progress off-screen."""
    db = FakeDb()
    ticks = []
    progress = []
    attempts = {"n": 0}

    def fake_dispatch(
        vm_id,
        command,
        params,
        *,
        job_id,
        role,
        timeout_s,
        secret_keys=(),
        expect_disconnect=False,
        on_progress=None,
        on_tick=None,
        client=None,
    ):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise agentbus.DispatchError("Server Manager is busy")
        if on_progress is not None:
            on_progress("installing the web role", 40.0)
        if on_tick is not None:
            on_tick(30.0)
        return {"ok": True}

    monkeypatch.setattr(agentbus, "dispatch_and_wait", fake_dispatch)

    steps = [
        Step(
            id="iis-share",
            command="iis.setup_certenroll",
            target="primary",
            retry_delays_s=(0,),
        )
    ]
    run_op_sequence(
        db,
        steps,
        _ctx(),
        plan_job_id="job-1",
        op_id="op-1",
        role="guest",
        on_step_progress=lambda step_id, phase, pct: progress.append((step_id, pct)),
        on_step_tick=lambda step_id, elapsed: ticks.append((step_id, elapsed)),
    )

    assert progress == [("iis-share", 40.0)]
    assert ticks == [("iis-share", 30.0)]
    # Metrics stay filed under the job key on purpose — `load_step_medians`
    # depends on the command-wide fallback for exactly this case.
    assert db["step_metrics"].docs[0]["stepId"] == "iis-share.retry.1"


def test_metrics_write_failure_never_fails_the_step(monkeypatch):
    db = FakeDb()
    db["step_metrics"].insert_one = lambda _doc: (_ for _ in ()).throw(
        RuntimeError("mongo down")
    )
    monkeypatch.setattr(agentbus, "dispatch_and_wait", lambda *a, **k: {"ok": True})
    steps = [Step(id="s1", command="dc.verify", target="primary")]
    results = run_op_sequence(
        db, steps, _ctx(), plan_job_id="job-1", op_id="op-1", role="guest"
    )
    assert results["s1"] == {"ok": True}


def test_step_persistence_atomically_merges_artifacts_instead_of_replacing_map():
    db = FakeDb()

    _persist_step(
        db,
        "job-1",
        "root-provision",
        "read-root-crt",
        {"contentB64": "Y2VydA=="},
        {"root_crt": "Y2VydA=="},
    )
    update = db["plan_runs"].last_update

    assert update["$set"]["artifacts.root_crt"] == "Y2VydA=="
    assert "artifacts" not in update["$set"]

    _persist_step(db, "job-1", "dc-provision", "dns-self", {"ok": True}, {})
    stale_update = db["plan_runs"].last_update
    assert not any(
        key == "artifacts" or key.startswith("artifacts.")
        for key in stale_update["$set"]
    )


# --------------------------------------------------------------------------- #
# load_step_medians / _step_median_seconds                                    #
# --------------------------------------------------------------------------- #
def _sample(db, command, step_id, ms, at):
    db["step_metrics"].insert_one(
        {"command": command, "stepId": step_id, "durationMs": ms, "at": at}
    )


def test_load_step_medians_takes_the_median_of_recent_runs():
    db = FakeDb()
    for i, ms in enumerate([10_000, 30_000, 20_000]):
        _sample(db, "dc.install_forest", "install-forest", ms, i)
    steps = [
        Step(id="install-forest", command="dc.install_forest", target="primary"),
        Step(id="never-ran", command="never.ran", target="primary"),
    ]
    assert load_step_medians(db, steps) == {"install-forest": 20_000.0}


def test_load_step_medians_uses_only_the_newest_sample_window():
    db = FakeDb()
    # 20 old slow runs, then 20 recent fast ones — only the recent window counts.
    for i in range(20):
        _sample(db, "cmd", "s1", 100_000, i)
    for i in range(20):
        _sample(db, "cmd", "s1", 5_000, 100 + i)
    steps = [Step(id="s1", command="cmd", target="primary")]
    assert load_step_medians(db, steps) == {"s1": 5_000.0}


def test_two_steps_sharing_a_command_do_not_share_a_median():
    """The real failure this keys off: `iis.setup_certenroll` publishes a share
    in ~20s as `iis-share` and stands up IIS in minutes as `iis-web`. The
    command-wide median handed `iis-web` the share's 21s estimate, which pinned
    its progress bar at the 95% cap five minutes into a real run."""
    db = FakeDb()
    for i, ms in enumerate([21_777, 40_707, 16_649]):
        _sample(db, "iis.setup_certenroll", "iis-share", ms, i)
    _sample(db, "iis.setup_certenroll", "iis-web", 600_000, 10)
    steps = [
        Step(id="iis-share", command="iis.setup_certenroll", target="primary"),
        Step(id="iis-web", command="iis.setup_certenroll", target="primary"),
    ]
    assert load_step_medians(db, steps) == {
        "iis-share": 21_777.0,
        "iis-web": 600_000.0,
    }


def test_a_step_with_no_samples_falls_back_to_its_command_median():
    """Metrics are filed under the *job key*, so a step that always takes a
    retry path (`install-forest` → `install-forest.postreboot`) has no samples
    under its own id and must still inherit its command's history."""
    db = FakeDb()
    for i, ms in enumerate([521_413, 536_388, 559_225]):
        _sample(db, "dc.install_forest", "install-forest.postreboot", ms, i)
    steps = [Step(id="install-forest", command="dc.install_forest", target="primary")]
    assert load_step_medians(db, steps) == {"install-forest": 536_388.0}


def test_step_median_seconds_maps_step_ids_and_converts_units():
    db = FakeDb()
    _sample(db, "dc.install_forest", "install-forest", 480_000, 1)
    steps = [
        Step(id="install-forest", command="dc.install_forest", target="primary"),
        Step(id="reboot", command="system.reboot", target="primary"),
    ]
    assert _step_median_seconds(db, steps) == {"install-forest": 480.0}


# --------------------------------------------------------------------------- #
# _sequence_progress: elapsed heartbeat + estimate math                       #
# --------------------------------------------------------------------------- #
def _progress(medians=None, total=3):
    state = {}
    pushes = []

    def push():
        pushes.append(state["op-1"])

    cbs = _sequence_progress("op-1", total, state, push, medians=medians)
    return cbs, pushes


def test_tick_shows_elapsed_and_is_throttled():
    (_, on_step_progress, on_step_tick), pushes = _progress()
    on_step_progress("install-forest", "installing AD DS forest", 10.0)
    on_step_tick("install-forest", 1.0)  # under the 10s throttle — no push
    on_step_tick("install-forest", 250.0)
    assert len(pushes) == 2
    last = pushes[-1]
    assert "Step 1/3 · install-forest: installing AD DS forest" in last.phase
    assert "4m 10s" in last.phase
    # No median for this command → elapsed text only, no estimate marker, and
    # the percent stays at the agent's last reported value (10% of step 1/3).
    assert "est." not in last.phase
    assert last.percent == pytest.approx(3.3, abs=0.1)


def test_tick_with_a_median_estimates_percent():
    (_, on_step_progress, on_step_tick), pushes = _progress(
        medians={"install-forest": 500.0}
    )
    on_step_progress("install-forest", "installing AD DS forest", 10.0)
    on_step_tick("install-forest", 250.0)  # half the median
    last = pushes[-1]
    assert "~50%" in last.phase and "est. 8m 20s" in last.phase
    # max(agent 10%, est 50%) = 50% of step 1/3 → 16.7% overall.
    assert last.percent == pytest.approx(16.7, abs=0.1)


def test_estimate_is_capped_below_completion():
    (_, _, on_step_tick), pushes = _progress(medians={"install-forest": 100.0}, total=1)
    on_step_tick("install-forest", 900.0)  # 9x the median
    last = pushes[-1]
    assert "~95%" in last.phase
    assert last.percent == pytest.approx(95.0, abs=0.1)


def test_first_ever_run_of_a_command_gets_elapsed_only():
    (_, _, on_step_tick), pushes = _progress(medians={}, total=1)
    on_step_tick("install-forest", 75.0)
    last = pushes[-1]
    assert "1m 15s" in last.phase
    assert "~" not in last.phase and "est." not in last.phase
    assert last.percent == 0.0


def test_fmt_duration():
    assert _fmt_duration(9) == "9s"
    assert _fmt_duration(60) == "1m"
    assert _fmt_duration(250) == "4m 10s"
    assert _fmt_duration(3725) == "1h 02m"
