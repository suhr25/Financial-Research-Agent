"""Accounts, sessions, demo access, and API protection."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth import service
from app.auth.service import SESSION_COOKIE, hash_password, verify_password
from app.main import app


@pytest.fixture(autouse=True)
def _clear_throttle():
    service.reset_throttle()
    yield
    service.reset_throttle()


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:8]}@example.com"


def test_password_hashing_round_trip_and_salting():
    h1, h2 = hash_password("correct horse"), hash_password("correct horse")
    assert h1 != h2  # per-user salt
    assert verify_password("correct horse", h1)
    assert not verify_password("wrong horse", h1)
    assert not verify_password("anything", "not-a-hash")


def test_protected_apis_require_a_session():
    client = TestClient(app)
    assert client.get("/api/industries").status_code == 401
    assert client.post("/api/research", json={"query": "Analyze TCS"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    # Public endpoints stay open.
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/public/overview").status_code == 200


def test_demo_session_grants_access_without_an_account():
    client = TestClient(app)
    resp = client.post("/api/auth/demo")
    assert resp.status_code == 200 and resp.json()["kind"] == "demo"
    assert client.get("/api/industries").status_code == 200
    assert client.get("/api/auth/me").json()["name"] == "Guest"


def test_signup_login_logout_flow():
    client = TestClient(app)
    email = _email()
    resp = client.post("/api/auth/signup", json={"name": "Asha", "email": email.upper(), "password": "s3cure-pass"})
    assert resp.status_code == 201
    assert resp.json()["email"] == email  # normalised to lower case
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/industries").status_code == 401

    resp = client.post("/api/auth/login", json={"email": email, "password": "s3cure-pass"})
    assert resp.status_code == 200 and resp.json()["kind"] == "user"
    assert client.get("/api/auth/me").json()["name"] == "Asha"


def test_session_token_is_not_stored_in_plain_text(db_session):
    from app.storage.models import SessionORM

    client = TestClient(app)
    client.post("/api/auth/demo")
    token = client.cookies.get(SESSION_COOKIE)
    assert token and db_session.get(SessionORM, token) is None


def test_signup_validation_and_duplicate_email():
    client = TestClient(app)
    assert client.post("/api/auth/signup", json={"name": "A", "email": "not-an-email", "password": "longenough"}).status_code == 400
    assert client.post("/api/auth/signup", json={"name": "A", "email": _email(), "password": "short"}).status_code == 400
    email = _email()
    assert client.post("/api/auth/signup", json={"name": "A", "email": email, "password": "longenough"}).status_code == 201
    assert client.post("/api/auth/signup", json={"name": "B", "email": email, "password": "longenough"}).status_code == 409


def test_wrong_password_and_unknown_email_get_the_same_answer():
    client = TestClient(app)
    email = _email()
    client.post("/api/auth/signup", json={"name": "A", "email": email, "password": "longenough"})
    a = client.post("/api/auth/login", json={"email": email, "password": "wrong-password"})
    b = client.post("/api/auth/login", json={"email": _email(), "password": "wrong-password"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


def test_repeated_failures_are_throttled():
    client = TestClient(app)
    email = _email()
    client.post("/api/auth/signup", json={"name": "A", "email": email, "password": "longenough"})
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": email, "password": "nope-nope"}).status_code == 401
    # Even the right password is refused while throttled.
    assert client.post("/api/auth/login", json={"email": email, "password": "longenough"}).status_code == 429


def test_public_overview_is_real_report_data_without_market_data():
    body = TestClient(app).get("/api/public/overview").json()
    assert body["companies"], "overview should list the industry's companies"
    first = body["companies"][0]
    assert {"revenue_ttm", "checks", "quarters_filed"} <= set(first)
    assert not {"price", "previous_close"} & set(first)
    assert not {"price_date", "total_market_cap"} & set(body)
    assert all("price" not in k["label"].lower() for k in first["checks"])
