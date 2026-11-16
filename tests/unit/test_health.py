# tests/unit/test_health.py
"""Unit tests for app/api/routes/health.py and the app's startup wiring."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import health
from app.main import app as real_app


# Readiness checks are async callables: return normally = healthy, raise = unhealthy.
async def check_ok() -> None:
    return None


async def check_boom() -> None:
    raise RuntimeError(
        "secret-db-password-in-message"
    )  # must never reach the response body


async def check_hangs() -> None:
    await asyncio.sleep(10)  # far longer than the timeout we set in the test


@pytest.fixture
def client() -> TestClient:
    """A tiny app with only the health router, so these tests don't depend on main.py."""
    app = FastAPI()
    app.include_router(health.router)
    app.state.readiness_checks = {}  # each test fills this as needed
    return TestClient(app)


# ---------------------------------------------------------------- /health --
def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ------------------------------------------------------------------ /ready --
def test_ready_with_no_checks_is_ready(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ready",
        "checks": {},
    }  # nothing registered -> nothing can fail


def test_ready_when_all_checks_pass(client):
    client.app.state.readiness_checks = {"db": check_ok, "cache": check_ok}
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"db": "ok", "cache": "ok"}


def test_ready_returns_503_when_one_check_fails(client):
    client.app.state.readiness_checks = {"db": check_ok, "cache": check_boom}
    r = client.get("/ready")
    assert r.status_code == 503  # load balancers stop routing on 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["checks"] == {
        "db": "ok",
        "cache": "fail",
    }  # healthy checks are still reported


def test_ready_does_not_leak_error_details(client):
    client.app.state.readiness_checks = {"db": check_boom}
    r = client.get("/ready")
    assert "secret" not in r.text  # exception text stays in the logs only


def test_ready_times_out_a_hung_check(client, monkeypatch):
    # Shrink the timeout so the test takes milliseconds instead of waiting 2 real seconds
    monkeypatch.setattr(health, "CHECK_TIMEOUT_S", 0.05)
    client.app.state.readiness_checks = {"cache": check_hangs}
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["checks"] == {"cache": "fail"}  # a timeout counts as a failure


# ---------------------------------------------------------------- real app --
def test_real_app_starts_and_serves_health():
    # `with` runs the lifespan: startup code before the block, shutdown code after it
    with TestClient(real_app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/ready").status_code == 200  # no checks registered yet in week 1
