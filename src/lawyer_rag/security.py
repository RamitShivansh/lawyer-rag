from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status
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


class MCPAuthMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

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
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not constant_time_equal(token, self.settings.mcp_token):
            await self._reject(send, 401, b"Invalid MCP bearer token", include_auth=True)
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        send: Callable[[Message], Awaitable[None]],
        status_code: int,
        body: bytes,
        *,
        include_auth: bool = False,
    ) -> None:
        headers = [(b"content-type", b"text/plain; charset=utf-8")]
        if include_auth:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body})
