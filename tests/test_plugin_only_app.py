from __future__ import annotations

from lawyer_rag.app import app


def test_custom_gpt_action_routes_are_not_registered() -> None:
    routes = {route.path for route in app.routes}

    assert "/gpt/openapi.json" not in routes
    assert "/gpt/privacy" not in routes
    assert not any(path.startswith("/api/gpt") for path in routes)
