"""What counts as orphaned — and, more importantly, what doesn't."""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.environments import (  # noqa: E402
    ABSENT_FROM_ESXI,
    AGENT_DEAD,
    STATUS_ERROR,
    STUCK_CLONING,
    classify_vm,
    is_purgeable,
    orphan_signals,
)

NOW = 1_800_000_000_000
DAY_MS = 86_400_000


def _row(**fields) -> dict:
    doc = {
        "vmName": "guest-alice-cc92f3-dc01",
        "status": "ready",
        "createdAt": NOW - DAY_MS,
        "updatedAt": NOW - DAY_MS,
    }
    doc.update(fields)
    return doc


def test_absence_is_never_asserted_when_the_inventory_could_not_be_read():
    # An ESXi outage must not make the whole playground look orphaned.
    assert orphan_signals(_row(), inventory=None, now=NOW) == []
    assert orphan_signals(_row(), inventory=set(), now=NOW) == [ABSENT_FROM_ESXI]


def test_a_deleted_row_is_not_flagged_for_being_absent():
    # Absent from the host is exactly what "deleted" means; it is not a fault.
    assert orphan_signals(_row(status="deleted"), inventory=set(), now=NOW) == []


def test_a_missing_agent_never_reads_as_a_dead_one():
    # Linux templates, authored ISOs, bundling switched off and every
    # post-teardown row legitimately carry no agent.
    for agent in (None, {}):
        signals = orphan_signals(
            _row(agent=agent), inventory={"guest-alice-cc92f3-dc01"}, now=NOW
        )
        assert AGENT_DEAD not in signals


def test_an_agent_that_never_connected_ages_out_from_when_it_was_minted():
    fresh = _row(agent={"vmId": "a", "mintedAt": NOW - 60_000})
    stale = _row(agent={"vmId": "a", "mintedAt": NOW - 3 * DAY_MS})
    inventory = {"guest-alice-cc92f3-dc01"}

    assert orphan_signals(fresh, inventory=inventory, now=NOW) == []
    assert orphan_signals(stale, inventory=inventory, now=NOW) == [AGENT_DEAD]


def test_last_connected_wins_over_minted_for_a_long_lived_agent():
    doc = _row(
        agent={"vmId": "a", "mintedAt": NOW - 30 * DAY_MS, "lastConnectedAt": NOW - 60}
    )
    assert orphan_signals(doc, inventory={"guest-alice-cc92f3-dc01"}, now=NOW) == []


def test_stuck_cloning_measures_from_the_last_write_not_creation():
    # createdAt is pinned by $setOnInsert, so a re-clone over an existing name
    # would look instantly stale if this measured from creation.
    doc = _row(status="cloning", createdAt=NOW - 30 * DAY_MS, updatedAt=NOW - 60_000)
    assert orphan_signals(doc, inventory={"guest-alice-cc92f3-dc01"}, now=NOW) == []

    doc["updatedAt"] = NOW - 2 * 3_600_000
    assert orphan_signals(doc, inventory={"guest-alice-cc92f3-dc01"}, now=NOW) == [
        STUCK_CLONING
    ]


def test_signals_accumulate_because_each_is_a_separate_reason():
    doc = _row(status="error", agent={"vmId": "a", "lastConnectedAt": NOW - 3 * DAY_MS})
    assert orphan_signals(doc, inventory=set(), now=NOW) == [
        ABSENT_FROM_ESXI,
        AGENT_DEAD,
        STATUS_ERROR,
    ]


def test_thresholds_are_configurable():
    doc = _row(agent={"vmId": "a", "lastConnectedAt": NOW - 7_200_000})
    inventory = {"guest-alice-cc92f3-dc01"}

    assert orphan_signals(doc, inventory=inventory, now=NOW) == []
    assert orphan_signals(
        doc, inventory=inventory, now=NOW, agent_dead_after_s=3_600
    ) == [AGENT_DEAD]


def test_a_row_is_purgeable_only_when_it_provably_backs_no_vm():
    present = _row()
    assert is_purgeable(present, inventory={"guest-alice-cc92f3-dc01"}) is False
    assert is_purgeable(present, inventory=set()) is True
    # An unreadable inventory proves nothing.
    assert is_purgeable(present, inventory=None) is False
    assert is_purgeable(_row(status="deleted"), inventory=None) is True


def test_classify_reports_agent_state_and_inventory_presence():
    live = classify_vm(
        _row(agent={"vmId": "a", "lastConnectedAt": NOW - 1_000}),
        inventory={"guest-alice-cc92f3-dc01"},
        now=NOW,
    )
    assert (live.agent_state, live.present_in_esxi, live.signals) == ("live", True, [])

    unknown = classify_vm(_row(), inventory=None, now=NOW)
    assert (unknown.agent_state, unknown.present_in_esxi) == ("none", None)
