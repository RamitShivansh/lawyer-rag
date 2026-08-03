# Apps SDK Migration Notes

This project is now packaged as a read-only ChatGPT app built around its existing Streamable HTTP
MCP server.

## What Changed

- `src/lawyer_rag/mcp_server.py` registers an Apps SDK widget resource:
  `ui://widget/legal-case-file.html`.
- The widget resource uses the required app MIME type: `text/html;profile=mcp-app`.
- Every MCP tool has read-only annotations and Apps SDK metadata that points to the widget.
- Every MCP tool advertises top-level OAuth `securitySchemes` for its read scope.
- The FastAPI app serves OAuth protected-resource metadata for the MCP resource.
- `search(query)` and `fetch(id)` expose the compatibility shape ChatGPT expects for broad
  retrieval and citation fetches.
- `.codex-plugin/plugin.json` and `.mcp.json` describe the project as a local plugin-backed MCP
  app for Codex/plugin workflows.

The existing matter-specific tools remain available for legal workflows:

- `list_matters`
- `list_documents`
- `search_case_file`
- `find_evidence`
- `get_citation`
- `read_document`

## Local Smoke Test

Start the app and worker as usual:

```bash
docker compose up --build -d
```

Connect MCP Inspector to:

```text
http://localhost:8000/mcp
```

Use this header:

```text
Authorization: Bearer <MCP_TOKEN>
```

Confirm:

- The tool list includes `search` and `fetch`.
- Tool descriptors include `readOnlyHint=true`.
- Tool descriptors include OAuth `securitySchemes`.
- Tool metadata points to `ui://widget/legal-case-file.html`.
- Reading the widget resource returns `text/html;profile=mcp-app`.

## ChatGPT Developer Mode

ChatGPT requires an HTTPS MCP endpoint. For development, expose the local app through a trusted
tunnel or deploy it to a temporary HTTPS host.

Use:

```text
MCP URL: https://your-domain.example/mcp
Authentication: Bearer token
Token: <MCP_TOKEN>
```

For the hosted OAuth plugin, configure Auth0 and set deployment values so URLs, widget CSP, and
the OAuth resource indicator match the public origin:

```text
BASE_URL=https://legalagentmcp.ramitshivansh.com
MCP_AUTH_MODE=oauth
OAUTH_ISSUER=https://your-tenant.auth0.com/
OAUTH_AUDIENCE=https://legalagentmcp.ramitshivansh.com/mcp
OAUTH_REQUIRED_SCOPES=matters:read documents:read evidence:search citations:read
TRUSTED_HOSTS=legalagentmcp.ramitshivansh.com
ALLOWED_ORIGINS=https://chatgpt.com,https://chat.openai.com,https://legalagentmcp.ramitshivansh.com
SECURE_COOKIES=true
```

Auth0 setup:

1. Create an API with identifier `https://legalagentmcp.ramitshivansh.com/mcp`.
2. Add permissions: `matters:read`, `documents:read`, `evidence:search`, `citations:read`.
3. Enable the MCP Resource Parameter Compatibility Profile and Include Issuer in Authorization
   Responses.
4. Create a Regular Web Application with Authorization Code flow and
   `client_secret_post`.
5. Add the exact ChatGPT callback URL shown in the plugin setup modal.

ChatGPT setup:

```text
MCP URL: https://legalagentmcp.ramitshivansh.com/mcp
Authentication: OAuth
Registration method: User-Defined OAuth Client
OAuth Client ID/Secret: values from Auth0
Token endpoint auth method: client_secret_post
```

Smoke-check metadata:

```bash
curl -i https://legalagentmcp.ramitshivansh.com/.well-known/oauth-protected-resource
curl -i https://legalagentmcp.ramitshivansh.com/.well-known/oauth-protected-resource/mcp
curl -i https://legalagentmcp.ramitshivansh.com/mcp/
```

An unauthenticated MCP request should return a `401` challenge with
`resource_metadata="https://legalagentmcp.ramitshivansh.com/.well-known/oauth-protected-resource/mcp"`.

If setting environment variables directly on the Python process, use the `LEGAL_RAG_` prefix.

## Plugin Packaging

The repo root contains:

- `.codex-plugin/plugin.json`
- `.mcp.json`

`.mcp.json` points at `http://localhost:8000/mcp` and reads the bearer token from
`LEGAL_RAG_MCP_TOKEN` for local plugin testing. For a hosted plugin package, update the MCP URL to
your HTTPS deployment and use OAuth in ChatGPT's plugin setup.

Do not add `.app.json` until a real submitted app/connector ID exists. The current manifest uses
only MCP server configuration, which is the part this repo can define locally.

## Security Boundary

Local development still uses a shared bearer token by default. Hosted ChatGPT plugin deployments
should use `MCP_AUTH_MODE=oauth` with Auth0.

This remains a single-operator corpus: any authenticated plugin user with the configured read
scopes can access the same matters and documents. Before broad multi-user distribution, add
tenant-aware authorization and ensure citation/source URLs re-check the caller's access.
