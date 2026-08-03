from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lawyer_rag.config import Settings


def constant_time_equal(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate.encode(), expected.encode())


def require_admin(request: Request) -> None:
    if request.session.get("admin") is not True:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin login required")


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


def verify_csrf(request: Request, token: str) -> None:
    expected = str(request.session.get("csrf", ""))
    if not expected or not constant_time_equal(token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def require_bearer_token(authorization: str, expected_token: str) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not constant_time_equal(token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class OAuthTokenError(Exception):
    """Raised when a bearer token cannot be accepted as an OAuth access token."""


class OAuthInsufficientScopeError(OAuthTokenError):
    """Raised when a valid OAuth access token does not grant the required scopes."""


class OAuthTokenVerifier:
    def __init__(self, settings: Settings, *, jwks_client: PyJWKClient | None = None):
        self.settings = settings
        self.jwks_client = jwks_client or PyJWKClient(settings.resolved_oauth_jwks_url)

    def validate_authorization(self, authorization: str) -> dict[str, Any]:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise OAuthTokenError("Missing bearer token")
        if not self.settings.oauth_issuer or not self.settings.oauth_audience:
            raise OAuthTokenError("OAuth issuer and audience must be configured")

        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.settings.oauth_audience,
                issuer=self.settings.oauth_issuer,
            )
        except jwt.PyJWTError as exc:
            raise OAuthTokenError("Invalid OAuth access token") from exc

        token_scopes = self._claim_scopes(claims)
        required_scopes = set(self.settings.oauth_scope_list)
        if not required_scopes.issubset(token_scopes):
            raise OAuthInsufficientScopeError("OAuth access token has insufficient scope")
        return claims

    @staticmethod
    def _claim_scopes(claims: dict[str, Any]) -> set[str]:
        scopes: set[str] = set()
        scope_claim = claims.get("scope", "")
        if isinstance(scope_claim, str):
            scopes.update(scope for scope in scope_claim.split() if scope)
        permissions_claim = claims.get("permissions", [])
        if isinstance(permissions_claim, list):
            scopes.update(scope for scope in permissions_claim if isinstance(scope, str))
        return scopes


class MCPAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        *,
        oauth_verifier: OAuthTokenVerifier | None = None,
    ):
        self.app = app
        self.settings = settings
        self.oauth_verifier = oauth_verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        origin = headers.get("origin")
        if origin and origin not in self.settings.allowed_origin_set:
            await self._reject(send, 403, b"Origin not allowed")
            return

        authorization = headers.get("authorization", "")
        mode = self.settings.mcp_auth_mode.lower()
        if mode not in {"bearer", "oauth", "oauth_or_bearer"}:
            await self._reject(send, 500, b"Invalid MCP auth mode")
            return

        if mode in {"bearer", "oauth_or_bearer"} and self._matches_local_bearer(authorization):
            await self.app(scope, receive, send)
            return

        if mode == "bearer":
            await self._reject(send, 401, b"Invalid MCP bearer token", include_bearer_auth=True)
            return

        verifier = self.oauth_verifier or OAuthTokenVerifier(self.settings)
        try:
            verifier.validate_authorization(authorization)
        except OAuthInsufficientScopeError:
            await self._reject(
                send,
                403,
                b"OAuth token has insufficient scope",
                oauth_error="insufficient_scope",
            )
            return
        except OAuthTokenError:
            await self._reject(send, 401, b"Invalid OAuth bearer token", include_oauth_auth=True)
            return

        await self.app(scope, receive, send)

    def _matches_local_bearer(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        return scheme.lower() == "bearer" and constant_time_equal(token, self.settings.mcp_token)

    def _oauth_challenge(self, *, error: str | None = None) -> str:
        parts = [
            "Bearer",
            f'resource_metadata="{self.settings.oauth_resource_metadata_url}"',
            f'scope="{" ".join(self.settings.oauth_scope_list)}"',
        ]
        if error:
            parts.append(f'error="{error}"')
        return " ".join(parts)

    async def _reject(
        self,
        send: Callable[[Message], Awaitable[None]],
        status_code: int,
        body: bytes,
        *,
        include_bearer_auth: bool = False,
        include_oauth_auth: bool = False,
        oauth_error: str | None = None,
    ) -> None:
        headers = [(b"content-type", b"text/plain; charset=utf-8")]
        if include_bearer_auth:
            headers.append((b"www-authenticate", b"Bearer"))
        if include_oauth_auth or oauth_error:
            headers.append(
                (
                    b"www-authenticate",
                    self._oauth_challenge(error=oauth_error).encode(),
                )
            )
        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body})
