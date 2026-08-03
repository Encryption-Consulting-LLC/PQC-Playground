"""One account's projects are invisible and untouchable to another.

Before ownership was enforced, ``list_projects`` was ``find({})`` and the single
-document routes matched on ``_id`` alone, so every account with the project
capabilities shared one global pool — reads, overwrites and deletes included.
"""

import asyncio
import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import AuthedUser, Role  # noqa: E402
from app.routers import projects  # noqa: E402
from app.routers.projects import ProjectIn  # noqa: E402

ALICE_PROJECT = "aaaaaaaa-abcd-4321-abcd-123456789abc"
BOB_PROJECT = "bbbbbbbb-abcd-4321-abcd-123456789abc"


class _FakeCollection:
    """Enough of a Mongo collection to exercise the filters, honouring every
    key in the query — which is the whole point of these tests."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def _matches(self, doc: dict, query: dict) -> bool:
        for key, expected in query.items():
            actual = doc.get("_id") if key == "_id" else doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    async def find_one(self, query: dict, projection: dict | None = None):
        for doc in self.docs.values():
            if self._matches(doc, query):
                return dict(doc)
        return None

    def find(self, query: dict, projection: dict | None = None):
        matched = [dict(d) for d in self.docs.values() if self._matches(d, query)]

        class _Cursor:
            def sort(self, *_args):
                return self

            async def to_list(self, length: int | None = None):
                return matched[:length]

        return _Cursor()

    async def insert_one(self, doc: dict):
        self.docs[doc["_id"]] = dict(doc)

    async def replace_one(self, query: dict, doc: dict):
        for key, existing in list(self.docs.items()):
            if self._matches(existing, query):
                self.docs[key] = dict(doc)
                return
        raise AssertionError("replace_one matched nothing — the filter disagreed")

    async def delete_one(self, query: dict):
        for key, doc in list(self.docs.items()):
            if self._matches(doc, query):
                del self.docs[key]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()


def _user(username: str, role: Role = Role.GUEST) -> AuthedUser:
    return AuthedUser(username=username, role=role, auth="local")


@pytest.fixture
def collection(monkeypatch) -> _FakeCollection:
    col = _FakeCollection()
    monkeypatch.setattr(projects, "projects_col", lambda: col)
    return col


def _create(project_id: str, user: AuthedUser, name: str = "Lab") -> dict:
    return asyncio.run(
        projects.create_project(ProjectIn(id=project_id, name=name), user)
    )


def test_a_created_project_records_its_owner(collection) -> None:
    created = _create(ALICE_PROJECT, _user("alice"))

    assert collection.docs[ALICE_PROJECT]["owner"] == "alice"
    assert created["id"] == ALICE_PROJECT


def test_listing_shows_only_the_callers_own_projects(collection) -> None:
    _create(ALICE_PROJECT, _user("alice"), "Alice's lab")
    _create(BOB_PROJECT, _user("bob"), "Bob's lab")

    listed = asyncio.run(projects.list_projects(_user("alice")))

    assert [p["name"] for p in listed["projects"]] == ["Alice's lab"]
    assert listed["count"] == 1


def test_an_operator_gets_no_cross_account_visibility(collection) -> None:
    """The rule has no per-role exception — an operator is scoped too."""
    _create(ALICE_PROJECT, _user("alice"))

    listed = asyncio.run(projects.list_projects(_user("olivia", Role.OPERATOR)))
    assert listed["projects"] == []

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(projects.get_project(ALICE_PROJECT, _user("olivia", Role.OPERATOR)))
    assert excinfo.value.status_code == 404


def test_reading_someone_elses_project_is_indistinguishable_from_a_miss(
    collection,
) -> None:
    """A 404 must not double as an oracle for whether a foreign id is real."""
    _create(ALICE_PROJECT, _user("alice"))
    nonexistent = "cccccccc-abcd-4321-abcd-123456789abc"

    with pytest.raises(HTTPException) as foreign:
        asyncio.run(projects.get_project(ALICE_PROJECT, _user("bob")))
    with pytest.raises(HTTPException) as absent:
        asyncio.run(projects.get_project(nonexistent, _user("bob")))

    assert foreign.value.status_code == absent.value.status_code == 404
    # Identical wording once the (caller-supplied) id is factored out.
    assert foreign.value.detail.replace(
        ALICE_PROJECT, "<id>"
    ) == absent.value.detail.replace(nonexistent, "<id>")


def test_another_account_cannot_overwrite_the_project(collection) -> None:
    _create(ALICE_PROJECT, _user("alice"), "Alice's lab")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            projects.update_project(
                ALICE_PROJECT, ProjectIn(name="hijacked"), _user("bob")
            )
        )

    assert excinfo.value.status_code == 404
    assert collection.docs[ALICE_PROJECT]["name"] == "Alice's lab"
    assert collection.docs[ALICE_PROJECT]["owner"] == "alice"


def test_an_update_reasserts_the_owner_from_the_session(collection) -> None:
    _create(ALICE_PROJECT, _user("alice"))

    asyncio.run(
        projects.update_project(
            ALICE_PROJECT, ProjectIn(name="Renamed"), _user("alice")
        )
    )

    assert collection.docs[ALICE_PROJECT]["owner"] == "alice"
    assert collection.docs[ALICE_PROJECT]["name"] == "Renamed"


def test_another_account_cannot_delete_the_project(collection) -> None:
    _create(ALICE_PROJECT, _user("alice"))

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(projects.delete_project(ALICE_PROJECT, _user("bob")))

    assert excinfo.value.status_code == 404
    assert ALICE_PROJECT in collection.docs

    asyncio.run(projects.delete_project(ALICE_PROJECT, _user("alice")))
    assert ALICE_PROJECT not in collection.docs


def test_a_legacy_ownerless_project_belongs_to_nobody(collection) -> None:
    """Documents written before ownership landed stay unreachable rather than
    falling to whoever asks first."""
    collection.docs[ALICE_PROJECT] = {
        "_id": ALICE_PROJECT,
        "name": "Pre-ownership lab",
        "owner": None,
        "createdAt": 1,
        "updatedAt": 1,
    }

    assert asyncio.run(projects.list_projects(_user("alice")))["projects"] == []
    with pytest.raises(HTTPException):
        asyncio.run(projects.get_project(ALICE_PROJECT, _user("alice")))


def test_a_guest_can_persist_projects_and_only_its_own() -> None:
    """Guests hold the project capabilities; the owner filter is the only limit.

    Asserted against ROLE_CAPABILITIES rather than a route, because the grant
    is only safe *because* every route above is scoped — the two belong together.
    """
    from app.core.authz import Capability, ROLE_CAPABILITIES

    assert Capability.PROJECT_READ in ROLE_CAPABILITIES[Role.GUEST]
    assert Capability.PROJECT_WRITE in ROLE_CAPABILITIES[Role.GUEST]
