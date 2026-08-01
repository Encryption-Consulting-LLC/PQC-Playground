"""Recovering attribution for VMs cloned before it was persisted.

The backfill's whole job is to be conservative: a wrong owner silently
misattributes a lab to someone who never deployed it, and the console offers
to destroy labs by owner.
"""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.cli import _backfill_fields  # noqa: E402

PROJECT_ID = "467893ab-cdef-4321-abcd-123456789abc"


def _row(vm_name: str = "guest-alice-cc92f3-dc01", **fields) -> dict:
    doc = {"vmName": vm_name, "jobId": "job-1"}
    doc.update(fields)
    return doc


def test_the_plan_run_beats_the_name_slug():
    # The run knows the real username; the name only knows a slug of it.
    fields = _backfill_fields(
        _row(),
        plan_owner="alice@corp.com",
        slug_owners={"alice": ["someone-else@corp.com"]},
    )

    assert fields["owner"] == "alice@corp.com"


def test_an_ambiguous_slug_writes_no_owner_but_still_writes_the_project_code():
    # Two accounts slug to "alice"; guessing one would misattribute the lab.
    # The project code is unambiguous either way, so it is still recovered.
    fields = _backfill_fields(
        _row(),
        slug_owners={"alice": ["alice@corp.com", "alice@partner.com"]},
    )

    assert "owner" not in fields
    assert fields["projectCode"] == "cc92f3"


def test_a_unique_slug_resolves_to_the_account():
    fields = _backfill_fields(_row(), slug_owners={"alice": ["alice@corp.com"]})

    assert fields == {"owner": "alice@corp.com", "projectCode": "cc92f3"}


def test_operator_names_carry_nothing_to_recover():
    assert (
        _backfill_fields(_row("lab-dc01"), slug_owners={"lab": ["ops@corp.com"]}) == {}
    )


def test_the_project_id_is_never_fabricated_from_the_name():
    # The name holds six characters of a UUID; writing that into projectId
    # would poison a field other code reads as a real id.
    fields = _backfill_fields(_row(), slug_owners={"alice": ["alice@corp.com"]})
    assert "projectId" not in fields

    from_run = _backfill_fields(
        _row(), plan_owner="alice@corp.com", plan_project_id=PROJECT_ID
    )
    assert from_run["projectId"] == PROJECT_ID


def test_a_second_run_is_a_no_op():
    already = _row(owner="alice@corp.com", projectCode="cc92f3", projectId=PROJECT_ID)

    assert (
        _backfill_fields(
            already,
            plan_owner="alice@corp.com",
            plan_project_id=PROJECT_ID,
            slug_owners={"alice": ["alice@corp.com"]},
        )
        == {}
    )


def test_partially_attributed_rows_are_topped_up_not_rewritten():
    row = _row(owner="alice@corp.com")

    fields = _backfill_fields(row, slug_owners={"alice": ["someone@corp.com"]})

    assert fields == {"projectCode": "cc92f3"}


def test_a_dashed_machine_name_yields_no_project_code():
    # ``web-01`` is a machine name, not a project — see the parse rule.
    fields = _backfill_fields(
        _row("guest-alice-web-01"), slug_owners={"alice": ["alice@corp.com"]}
    )

    assert fields == {"owner": "alice@corp.com"}
