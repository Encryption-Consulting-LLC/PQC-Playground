"""Registry rows reassemble into the labs they were deployed as."""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.environments import (  # noqa: E402
    UNGROUPED_KEY,
    group_environments,
)

NOW = 1_800_000_000_000


def _row(vm_name: str, **fields) -> dict:
    doc = {
        "vmName": vm_name,
        "appName": fields.pop("appName", vm_name),
        "status": "ready",
        "ip": "10.0.20.51",
        "createdAt": NOW - 60_000,
        "updatedAt": NOW - 60_000,
    }
    doc.update(fields)
    return doc


def test_field_backed_and_name_parsed_rows_land_in_one_group():
    # A lab deployed before ownership was persisted and one deployed after must
    # not show up as two environments.
    envs = group_environments(
        [
            _row(
                "guest-alice-cc92f3-dc01", owner="alice@corp.com", projectCode="cc92f3"
            ),
            _row("guest-alice-cc92f3-ca01"),  # legacy: identity only in the name
        ],
        inventory=None,
        slug_owners={"alice": ["alice@corp.com"]},
        now=NOW,
    )

    assert len(envs) == 1
    assert envs[0].owner == "alice@corp.com"
    assert envs[0].owner_resolved is True
    assert envs[0].project_code == "cc92f3"
    assert [vm.vm_name for vm in envs[0].vms] == [
        "guest-alice-cc92f3-ca01",
        "guest-alice-cc92f3-dc01",
    ]


def test_an_unmatched_slug_stays_a_slug_rather_than_being_guessed():
    envs = group_environments(
        [_row("guest-alice-cc92f3-dc01")],
        inventory=None,
        slug_owners={},
        now=NOW,
    )

    assert envs[0].owner == "alice"
    assert envs[0].owner_resolved is False
    assert envs[0].owner_ambiguous is False


def test_two_accounts_colliding_on_a_slug_leave_the_group_ambiguous():
    envs = group_environments(
        [_row("guest-alice-cc92f3-dc01")],
        inventory=None,
        slug_owners={"alice": ["alice@corp.com", "alice@partner.com"]},
        now=NOW,
    )

    assert envs[0].owner == "alice"
    assert envs[0].owner_resolved is False
    assert envs[0].owner_ambiguous is True


def test_operator_and_legacy_rows_share_one_ungrouped_bucket():
    envs = group_environments(
        [_row("lab-dc01"), _row("ws-test-2"), _row("guest-alice-cc92f3-dc01")],
        inventory=None,
        now=NOW,
    )

    ungrouped = [env for env in envs if env.key == UNGROUPED_KEY]
    assert len(ungrouped) == 1
    assert ungrouped[0].vm_count == 2
    assert ungrouped[0].owner is None
    # The catch-all sorts last, behind every real environment.
    assert envs[-1].key == UNGROUPED_KEY


def test_projectless_guest_vms_group_separately_from_a_project_lab():
    envs = group_environments(
        [
            _row("guest-alice-cc92f3-dc01"),
            _row("guest-alice-scratch"),  # direct clone, no project segment
        ],
        inventory=None,
        now=NOW,
    )

    assert {env.project_code for env in envs} == {"cc92f3", None}
    assert all(env.vm_count == 1 for env in envs)


def test_deleted_rows_are_hidden_unless_asked_for():
    docs = [
        _row("guest-alice-cc92f3-dc01"),
        _row("guest-alice-cc92f3-ca01", status="deleted", ip=None),
    ]

    assert group_environments(docs, inventory=None, now=NOW)[0].vm_count == 1
    assert (
        group_environments(docs, inventory=None, now=NOW, include_deleted=True)[
            0
        ].vm_count
        == 2
    )


def test_rollups_count_addresses_live_agents_and_flags():
    envs = group_environments(
        [
            _row(
                "guest-alice-cc92f3-dc01",
                agent={"vmId": "a", "lastConnectedAt": NOW - 1_000},
            ),
            _row(
                "guest-alice-cc92f3-ca01",
                agent={"vmId": "b", "lastConnectedAt": NOW - 90_000_000},
            ),
            _row("guest-alice-cc92f3-web01", ip=None),  # no agent at all
        ],
        inventory={"guest-alice-cc92f3-dc01", "guest-alice-cc92f3-ca01"},
        now=NOW,
    )

    env = envs[0]
    assert env.vm_count == 3
    assert env.ip_count == 2
    assert (env.agents_live, env.agents_total) == (1, 2)
    # The stale agent, plus the web host that is missing from the host entirely.
    assert env.flagged == 2


def test_environments_sort_newest_first():
    envs = group_environments(
        [
            _row("guest-alice-aaaaaa-dc01", createdAt=NOW - 500_000),
            _row("guest-alice-bbbbbb-dc01", createdAt=NOW - 100_000),
        ],
        inventory=None,
        now=NOW,
    )

    assert [env.project_code for env in envs] == ["bbbbbb", "aaaaaa"]
