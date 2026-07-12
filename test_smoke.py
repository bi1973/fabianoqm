import os

os.environ.setdefault("SIGNING_SECRET", "test-secret-that-is-long-enough-for-tests")

from app import app  # noqa: E402


def test_root_status():
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    data = response.get_json()
    assert data["mode"] == "controlled-read-write"
    assert data["delete_enabled"] is False


def test_health():
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 200


def test_api_requires_oauth():
    client = app.test_client()
    response = client.get("/api/profile")
    assert response.status_code == 401
