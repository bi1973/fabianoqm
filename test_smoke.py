import os
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("SIGNING_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("PUBLIC_BASE_URL", "https://mendeley-controlled-writer.onrender.com")
os.environ.setdefault("CHATGPT_CALLBACK_HOST", "chatgpt.com")

import app as bridge_app  # noqa: E402

app = bridge_app.app


def test_root_status():
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    data = response.get_json()
    assert data["mode"] == "controlled-read-write"
    assert data["delete_enabled"] is False
    assert data["version"] == "1.0.2"


def test_health():
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 200


def test_api_requires_oauth():
    client = app.test_client()
    response = client.get("/api/profile")
    assert response.status_code == 401


def test_oauth_authorize_uses_bridge_callback_and_returns_to_chatgpt_com():
    client = app.test_client()
    legacy_callback = "https://chat.openai.com/aip/g-test123/oauth/callback"
    response = client.get(
        "/oauth/authorize",
        query_string={
            "response_type": "code",
            "client_id": "24023",
            "redirect_uri": legacy_callback,
            "state": "chatgpt-state-123",
            "scope": "all",
        },
    )
    assert response.status_code == 302
    upstream = urlparse(response.headers["Location"])
    upstream_query = parse_qs(upstream.query)
    assert upstream.netloc == "api.mendeley.com"
    assert upstream.path == "/oauth/authorize"
    assert upstream_query["redirect_uri"] == [
        "https://mendeley-controlled-writer.onrender.com/oauth/callback"
    ]
    relay_state = upstream_query["state"][0]

    callback_response = client.get(
        "/oauth/callback",
        query_string={"code": "provider-code", "state": relay_state},
    )
    assert callback_response.status_code == 303
    returned = urlparse(callback_response.headers["Location"])
    returned_query = parse_qs(returned.query)
    assert f"{returned.scheme}://{returned.netloc}{returned.path}" == (
        "https://chatgpt.com/aip/g-test123/oauth/callback"
    )
    assert returned_query["code"] == ["provider-code"]
    assert returned_query["state"] == ["chatgpt-state-123"]
    assert callback_response.headers["Cache-Control"] == "no-store"


def test_oauth_keeps_current_chatgpt_callback_host():
    client = app.test_client()
    current_callback = "https://chatgpt.com/aip/g-test456/oauth/callback"
    response = client.get(
        "/oauth/authorize",
        query_string={
            "response_type": "code",
            "client_id": "24023",
            "redirect_uri": current_callback,
            "state": "chatgpt-state-456",
            "scope": "all",
        },
    )
    relay_state = parse_qs(urlparse(response.headers["Location"]).query)["state"][0]
    callback_response = client.get(
        "/oauth/callback",
        query_string={"code": "provider-code", "state": relay_state},
    )
    returned = urlparse(callback_response.headers["Location"])
    assert f"{returned.scheme}://{returned.netloc}{returned.path}" == current_callback


def test_oauth_rejects_untrusted_callback():
    client = app.test_client()
    response = client.get(
        "/oauth/authorize",
        query_string={
            "response_type": "code",
            "client_id": "24023",
            "redirect_uri": "https://example.com/steal",
            "state": "state",
            "scope": "all",
        },
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_chatgpt_callback"


def test_token_exchange_rewrites_redirect_uri(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b'{"access_token":"redacted","token_type":"bearer"}'
        headers = {"Content-Type": "application/json"}

    def fake_post(url, headers, data, auth, timeout):
        captured.update(
            {"url": url, "headers": headers, "data": data, "auth": auth, "timeout": timeout}
        )
        return FakeResponse()

    monkeypatch.setattr(bridge_app.requests, "post", fake_post)
    client = app.test_client()
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "provider-code",
            "redirect_uri": "https://chat.openai.com/aip/g-test123/oauth/callback",
            "client_id": "24023",
            "client_secret": "test-secret",
        },
    )
    assert response.status_code == 200
    assert captured["data"]["redirect_uri"] == (
        "https://mendeley-controlled-writer.onrender.com/oauth/callback"
    )
    assert captured["auth"] == ("24023", "test-secret")
    assert response.headers["Cache-Control"] == "no-store"
