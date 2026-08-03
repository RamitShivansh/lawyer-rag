# Legal Case File RAG Apps SDK Server

An OCR-first, matter-scoped evidence service for scanned legal case files. Operators upload
PDFs in a small admin UI; ChatGPT and trusted agents retrieve passages through read-only
Apps SDK-compatible MCP tools. The service does not generate legal answers or store generated
summaries.

## Architecture

- PostgreSQL is the source of truth for matters, documents, OCR pages, chunks, and jobs.
- Qdrant stores BM25 sparse vectors and BGE dense vectors with matter filters.
- A CPU worker runs OCRmyPDF/Tesseract, legal-aware chunking, embedding, and indexing.
- FastAPI hosts the admin UI, citation viewer, health checks, and authenticated Apps SDK MCP
  endpoint.
- Models are downloaded while the image is built and loaded locally at runtime.

The design takes architectural inspiration from
[RAG-MCP-Server](https://github.com/RamitShivansh/RAG-MCP-Server) but does not use its Chroma,
LangChain, mutable MCP tools, or fixed-size PDF pipeline.

## Start with Docker

1. Create deployment secrets:

   ```bash
   cp .env.example .env
   openssl rand -hex 32
   ```

   Put a different random value in each secret field. Keep `BIND_ADDRESS=127.0.0.1` unless
   the service is reachable only through a trusted LAN or VPN. When using HTTPS, set
   `BASE_URL`, `ALLOWED_ORIGINS`, `TRUSTED_HOSTS`, and `SECURE_COOKIES=true` accordingly.

2. Build and start:

   ```bash
   docker compose up --build -d
   docker compose ps
   ```

   The first build downloads and bakes the three local retrieval models. Runtime containers
   do not call external model APIs.

3. Open <http://localhost:8000/admin>, enter `ADMIN_TOKEN`, create a matter, and upload the
   PDFs from `test_files/`. OCR happens asynchronously; the dashboard reports page progress.

4. Inspect structured logs without document contents:

   ```bash
   docker compose logs -f api worker
   ```

## Apps SDK connection

- URL: `http://localhost:8000/mcp`
- Transport: stateless Streamable HTTP
- Header: `Authorization: Bearer <MCP_TOKEN>`

The MCP server now exposes:

- An Apps SDK widget resource at `ui://widget/legal-case-file.html` with
  `text/html;profile=mcp-app`.
- Read-only tool annotations and widget metadata on every tool descriptor.
- Compatibility `search(query)` and `fetch(id)` tools for ChatGPT app/company-knowledge-style
  retrieval.
- Repo-local plugin metadata in `.codex-plugin/plugin.json` and `.mcp.json`.

For local Codex/plugin testing, expose the same token to the Codex process and add the
Streamable HTTP endpoint, or use the checked-in `.mcp.json` from a plugin install:

```bash
export LEGAL_RAG_MCP_TOKEN='<same value as MCP_TOKEN in .env>'
codex mcp add legal-rag \
  --url http://localhost:8000/mcp \
  --bearer-token-env-var LEGAL_RAG_MCP_TOKEN
codex mcp get legal-rag
```

Restart Codex after adding the server. If Codex is launched from a desktop icon rather than
the shell, make sure the desktop process receives `LEGAL_RAG_MCP_TOKEN`; do not commit the
token in a project-level configuration file.

Available tools:

- `search`
- `fetch`
- `list_matters`
- `list_documents`
- `search_case_file`
- `find_evidence`
- `get_citation`
- `read_document`

The `case_summary_workflow` MCP prompt instructs an agent to traverse every ready document
and disclose incomplete or low-quality sources. `find_evidence` returns candidates; the agent
must decide whether each passage supports, contradicts, or is neutral toward the proposition.

Use MCP Inspector against the URL above and configure the bearer header in its connection
settings. A conforming client must send the negotiated `MCP-Protocol-Version` header; the MCP
SDK handles validation.

## ChatGPT developer-mode app

For ChatGPT Apps SDK testing, host the server at an HTTPS URL and connect the remote MCP endpoint
from ChatGPT developer mode:

- MCP URL: `https://your-domain.example/mcp`
- Authentication: bearer token using `MCP_TOKEN`
- Widget resource: `ui://widget/legal-case-file.html`

Production/public distribution needs a submitted plugin and a stable HTTPS deployment. For this
single-operator MVP, keep the shared bearer token private and only connect deployments protected
by your own network, tunnel, or hosting controls. See [docs/apps-sdk.md](docs/apps-sdk.md) for
the migration notes and deployment checklist.

## Development

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn lawyer_rag.app:app --reload
uv run lawyer-rag-worker
uv run pytest
uv run ruff check .
```

Local development defaults to SQLite, but Docker and production use PostgreSQL. Qdrant must
be available at the configured URL. Tesseract, Ghostscript, qpdf, unpaper, and Poppler are
included in the image.

## Evaluation

The evaluation input is JSONL:

```json
{"matter_id":"...","query":"Was notice served?","relevant_citation_ids":["cite:..."]}
```

Run:

```bash
docker compose exec api lawyer-rag-eval /data/evaluation.jsonl
```

The report compares lexical, dense, hybrid, and reranked retrieval using Recall@10, MRR@10,
and nDCG@10. Project acceptance targets are Recall@10 ≥ 0.85 and MRR@10 ≥ 0.70 on the
operator-supplied evaluation set.

## Backups and upgrades

- Back up PostgreSQL with `pg_dump`, Qdrant through snapshots, and the `document_data` volume.
- Restore all three from the same backup point; originals in `document_data` are immutable.
- Run `docker compose run --rm migrate alembic upgrade head` before starting a new image.
- Parser or embedding changes use a new Qdrant collection name and alias switch after a full
  reindex. Never mix embedding dimensions in an existing collection.
- Archiving removes a document from retrieval but retains its original and audit metadata.

## Security boundary

This MVP is for one trusted operator on a private network. PostgreSQL and Qdrant are not
published to the host. MCP cannot upload, rename, archive, or delete documents. Uploaded
filenames are metadata only; storage paths are UUID-based. PDF URL ingestion is intentionally
unsupported.
