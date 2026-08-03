from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from lawyer_rag.config import Settings
from lawyer_rag.security import MCPAuthMiddleware, OAuthTokenVerifier


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def _call_middleware(
    settings: Settings,
    *,
    authorization: str | None = None,
    oauth_verifier: OAuthTokenVerifier | None = None,
) -> list[Message]:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = MCPAuthMiddleware(_ok_app, settings, oauth_verifier=oauth_verifier)
    await middleware(scope, receive, send)
    return messages


class StaticJwksClient:
    def __init__(self, public_key: Any):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> SimpleNamespace:
        return SimpleNamespace(key=self.public_key)


@pytest.fixture
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _oauth_settings(*, mode: str = "oauth", scopes: str | None = None) -> Settings:
    return Settings(
        mcp_auth_mode=mode,
        mcp_token="local-secret",  # noqa: S106
        base_url="https://legalagentmcp.ramitshivansh.com",
        oauth_issuer="https://example.auth0.com/",
        oauth_audience="https://legalagentmcp.ramitshivansh.com/mcp",
        oauth_required_scopes=scopes
        or "matters:read documents:read evidence:search citations:read",
    )


def _signed_token(
    private_key,
    *,
    issuer: str = "https://example.auth0.com/",
    audience: str = "https://legalagentmcp.ramitshivansh.com/mcp",
    scope: str = "matters:read documents:read evidence:search citations:read",
    expires_delta: timedelta = timedelta(minutes=5),
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + expires_delta,
            "scope": scope,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def _verifier(settings: Settings, private_key) -> OAuthTokenVerifier:
    return OAuthTokenVerifier(
        settings,
        jwks_client=StaticJwksClient(private_key.public_key()),
    )


def _headers(messages: list[Message]) -> dict[str, str]:
    start = messages[0]
    return {key.decode().lower(): value.decode() for key, value in start["headers"]}


def test_oauth_protected_resource_metadata_uses_mcp_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lawyer_rag import app as app_module

    monkeypatch.setattr(app_module.settings, "base_url", "https://legalagentmcp.ramitshivansh.com")
    monkeypatch.setattr(app_module.settings, "oauth_issuer", "https://example.auth0.com/")
    monkeypatch.setattr(
        app_module.settings,
        "oauth_audience",
        "https://legalagentmcp.ramitshivansh.com/mcp",
    )
    monkeypatch.setattr(
        app_module.settings,
        "oauth_required_scopes",
        "matters:read documents:read evidence:search citations:read",
    )

    client = TestClient(app_module.app, base_url="http://localhost")

    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "resource": "https://legalagentmcp.ramitshivansh.com/mcp",
            "authorization_servers": ["https://example.auth0.com"],
            "scopes_supported": [
                "matters:read",
                "documents:read",
                "evidence:search",
                "citations:read",
            ],
        }


@pytest.mark.asyncio
async def test_oauth_mode_missing_token_returns_metadata_challenge() -> None:
    settings = _oauth_settings()

    messages = await _call_middleware(settings)

    assert messages[0]["status"] == 401
    authenticate = _headers(messages)["www-authenticate"]
    expected_metadata = (
        'resource_metadata="'
        "https://legalagentmcp.ramitshivansh.com/.well-known/oauth-protected-resource/mcp"
        '"'
    )
    assert expected_metadata in authenticate
    assert 'scope="matters:read documents:read evidence:search citations:read"' in authenticate


@pytest.mark.asyncio
async def test_oauth_mode_rejects_valid_token_without_required_scope(private_key) -> None:
    settings = _oauth_settings(scopes="matters:read documents:read")
    token = _signed_token(private_key, scope="matters:read")

    messages = await _call_middleware(
        settings,
        authorization=f"Bearer {token}",
        oauth_verifier=_verifier(settings, private_key),
    )

    assert messages[0]["status"] == 403
    authenticate = _headers(messages)["www-authenticate"]
    assert 'error="insufficient_scope"' in authenticate
    assert 'scope="matters:read documents:read"' in authenticate


@pytest.mark.asyncio
async def test_oauth_mode_accepts_valid_auth0_access_token(private_key) -> None:
    settings = _oauth_settings(scopes="evidence:search")
    token = _signed_token(private_key, scope="openid profile evidence:search")

    messages = await _call_middleware(
        settings,
        authorization=f"Bearer {token}",
        oauth_verifier=_verifier(settings, private_key),
    )

    assert messages[0]["status"] == 200


@pytest.mark.asyncio
async def test_oauth_mode_rejects_wrong_audience(private_key) -> None:
    settings = _oauth_settings(scopes="evidence:search")
    token = _signed_token(
        private_key,
        audience="https://other.example.com/mcp",
        scope="evidence:search",
    )

    messages = await _call_middleware(
        settings,
        authorization=f"Bearer {token}",
        oauth_verifier=_verifier(settings, private_key),
    )

    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_oauth_mode_rejects_wrong_issuer(private_key) -> None:
    settings = _oauth_settings(scopes="evidence:search")
    token = _signed_token(
        private_key,
        issuer="https://other.auth0.com/",
        scope="evidence:search",
    )

    messages = await _call_middleware(
        settings,
        authorization=f"Bearer {token}",
        oauth_verifier=_verifier(settings, private_key),
    )

    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_oauth_mode_rejects_expired_token(private_key) -> None:
    settings = _oauth_settings(scopes="evidence:search")
    token = _signed_token(
        private_key,
        scope="evidence:search",
        expires_delta=timedelta(minutes=-5),
    )

    messages = await _call_middleware(
        settings,
        authorization=f"Bearer {token}",
        oauth_verifier=_verifier(settings, private_key),
    )

    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_oauth_mode_rejects_bad_signature(private_key) -> None:
    settings = _oauth_settings(scopes="evidence:search")
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _signed_token(other_private_key, scope="evidence:search")

    messages = await _call_middleware(
        settings,
        authorization=f"Bearer {token}",
        oauth_verifier=_verifier(settings, private_key),
    )

    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_oauth_or_bearer_mode_accepts_local_bearer_token() -> None:
    settings = _oauth_settings(mode="oauth_or_bearer")

    messages = await _call_middleware(settings, authorization="Bearer local-secret")

    assert messages[0]["status"] == 200
