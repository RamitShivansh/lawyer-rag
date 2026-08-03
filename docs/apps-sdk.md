# Apps SDK Migration Notes

This project is now packaged as a read-only ChatGPT app built around its existing Streamable HTTP
MCP server.

## What Changed

- `src/lawyer_rag/mcp_server.py` registers an Apps SDK widget resource:
  `ui://widget/legal-case-file.html`.
- The widget resource uses the required app MIME type: `text/html;profile=mcp-app`.
- Every MCP tool has read-only annotations and Apps SDK metadata that points to the widget.
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

Set deployment values so URLs and widget CSP match the public origin:

```text
BASE_URL=https://your-domain.example
TRUSTED_HOSTS=your-domain.example
ALLOWED_ORIGINS=https://chatgpt.com,https://chat.openai.com,https://your-domain.example
SECURE_COOKIES=true
```

If setting environment variables directly on the Python process, use the `LEGAL_RAG_` prefix.

## Plugin Packaging

The repo root contains:

- `.codex-plugin/plugin.json`
- `.mcp.json`

`.mcp.json` points at `http://localhost:8000/mcp` and reads the bearer token from
`LEGAL_RAG_MCP_TOKEN`. For a hosted plugin package, update the MCP URL to your HTTPS deployment
before distribution.

Do not add `.app.json` until a real submitted app/connector ID exists. The current manifest uses
only MCP server configuration, which is the part this repo can define locally.

## Security Boundary

This MVP still uses a shared bearer token and single-operator admin model. Keep it private.

Before broad/public app distribution, replace shared token auth with per-user OAuth, enforce
tenant-aware authorization on every MCP request, and ensure citation/source URLs re-check the
caller's access.
