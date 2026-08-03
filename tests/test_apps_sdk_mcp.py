from __future__ import annotations

import pytest

from lawyer_rag.mcp_server import APP_RESOURCE_MIME_TYPE, APP_RESOURCE_URI, mcp


def test_apps_sdk_widget_resource_is_registered() -> None:
    resources = {str(resource.uri): resource for resource in mcp._resource_manager.list_resources()}

    resource = resources[APP_RESOURCE_URI]

    assert resource.mime_type == APP_RESOURCE_MIME_TYPE
    assert resource.meta["ui"]["domain"]
    assert resource.meta["ui"]["csp"]["connectDomains"]
    assert "openai/widgetDescription" in resource.meta


def test_apps_sdk_tools_have_read_only_annotations_and_widget_meta() -> None:
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    assert {
        "fetch",
        "find_evidence",
        "get_citation",
        "list_documents",
        "list_matters",
        "read_document",
        "search",
        "search_case_file",
    }.issubset(tools)

    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.meta["ui"]["resourceUri"] == APP_RESOURCE_URI
        assert tool.meta["openai/outputTemplate"] == APP_RESOURCE_URI
        assert tool.fn_metadata.output_schema is not None


def test_search_and_fetch_match_chatgpt_app_compatibility_shapes() -> None:
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    assert tools["search"].parameters["required"] == ["query"]
    assert tools["search"].fn_metadata.output_schema["properties"]["results"]["type"] == "array"
    assert tools["fetch"].parameters["required"] == ["id"]
    assert {"id", "title", "text", "url"}.issubset(
        tools["fetch"].fn_metadata.output_schema["properties"]
    )


@pytest.mark.asyncio
async def test_apps_sdk_tools_expose_oauth_security_schemes() -> None:
    tools = {tool.name: tool.model_dump(by_alias=True) for tool in await mcp.list_tools()}

    assert tools["list_matters"]["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["matters:read"]}
    ]
    assert tools["read_document"]["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["documents:read"]}
    ]
    assert tools["search_case_file"]["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["evidence:search"]}
    ]
    assert tools["fetch"]["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["citations:read"]}
    ]
