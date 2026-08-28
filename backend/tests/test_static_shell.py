"""The SPA shells must not be cached; the hashed assets beside them may be.

A shell served without `Cache-Control` gets heuristic freshness from
`Last-Modified`, so a browser can keep running yesterday's bundle after a deploy
— the origin serves the new shell and the tab never asks for it. That failure
looks exactly like "the fix did not work", which is what it cost to learn.
"""

import os

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("SETTINGS_ENC_KEY", "0" * 44)

from pathlib import Path  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


def _built_spa(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    return dist


def test_the_spa_shells_are_served_uncacheable(tmp_path, monkeypatch):
    frontend = _built_spa(tmp_path / "frontend")
    admin = _built_spa(tmp_path / "admin")
    monkeypatch.setenv("FRONTEND_DIST", str(frontend))
    monkeypatch.setenv("ADMIN_DIST", str(admin))

    client = TestClient(create_app())
    for path in ("/", "/some/spa/route", "/admin", "/admin/registry"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-store" in response.headers.get("cache-control", ""), path


def test_hashed_assets_keep_their_own_caching(tmp_path, monkeypatch):
    """Only the shell is uncacheable. The assets are content-addressed, so
    forbidding their caching would make every navigation re-download the app."""

    frontend = _built_spa(tmp_path / "frontend")
    monkeypatch.setenv("FRONTEND_DIST", str(frontend))

    client = TestClient(create_app())
    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "no-store" not in response.headers.get("cache-control", "")
