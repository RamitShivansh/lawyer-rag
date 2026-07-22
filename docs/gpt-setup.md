# Custom GPT Setup

Use this guide after the application is hosted on your HTTPS domain.

## GPT Builder Fields

Name:

```text
Legal Case File RAG
```

Description:

```text
Search and read uploaded legal case files with citation-grounded answers from a private Legal Case File RAG deployment.
```

Instructions:

```text
You are a legal case-file research assistant connected to a private Legal Case File RAG deployment.

Core behavior:
- Use the action API for matter-specific facts. Do not invent facts that are not supported by retrieved matter documents.
- Treat retrieved OCR text as source evidence, not as legal advice or final truth.
- Cite factual claims with citation IDs, document names, and page numbers whenever available.
- Distinguish allegations, party submissions, evidence, judicial findings, and operative orders.
- State uncertainty, conflicts, missing documents, processing failures, archived documents, and low-OCR-quality warnings.
- If the user asks for a full case summary, first list documents for the matter, then read ready documents in page order until complete.
- If the user asks whether a proposition is supported, use findEvidence and explain whether each strong passage supports, contradicts, or is neutral.
- Keep uploads, deletion, archiving, retention, and admin tasks out of ChatGPT. Direct the user to the hosted admin portal for those tasks.
- This GPT is a retrieval aid for professional legal work. Remind users to have outputs reviewed by a qualified person before use.
```

Conversation starters:

```text
List the available matters.
Summarize the ready documents in this matter with citations.
Find evidence for and against this proposition.
Read this document from page 1 and summarize the key procedural history.
Resolve this citation and show the surrounding context.
```

Recommended capabilities:

```text
Actions: enabled
Web search: disabled
Image generation: disabled
Code Interpreter & Data Analysis: optional
Canvas: optional
Apps: disabled, because GPTs cannot use Apps and Actions at the same time
```

## Action Configuration

In the GPT editor, create a new action.

Authentication:

```text
API key
Bearer
```

API key value:

```text
<your LEGAL_RAG_MCP_TOKEN / MCP_TOKEN value>
```

OpenAPI schema URL:

```text
https://your-domain.example/gpt/openapi.json
```

Privacy policy URL:

```text
https://your-domain.example/gpt/privacy
```

## Hosting Requirements

Set these deployment values before importing the schema. If you use `compose.yaml`, put the
unprefixed names in `.env` because Compose maps them into the container:

```text
BASE_URL=https://your-domain.example
TRUSTED_HOSTS=your-domain.example
ALLOWED_ORIGINS=https://chatgpt.com,https://chat.openai.com,https://your-domain.example
SECURE_COOKIES=true
```

If you set environment variables directly on the application process, use the prefixed names:

```text
LEGAL_RAG_BASE_URL=https://your-domain.example
LEGAL_RAG_TRUSTED_HOSTS=your-domain.example
LEGAL_RAG_ALLOWED_ORIGINS=https://chatgpt.com,https://chat.openai.com,https://your-domain.example
LEGAL_RAG_SECURE_COOKIES=true
```

Keep `LEGAL_RAG_MCP_TOKEN` secret. Anyone with the token can call the GPT action API and read
the matters exposed by this single-operator deployment.

## Published Endpoints

- `GET /gpt/openapi.json`: dynamic OpenAPI schema for GPT Actions.
- `GET /gpt/privacy`: privacy policy URL required for shared/public GPTs with actions.
- `GET /api/gpt/matters`: list matters.
- `GET /api/gpt/matters/{matter_id}/documents`: list documents and ingestion status.
- `POST /api/gpt/search`: search citation-ready evidence.
- `POST /api/gpt/find-evidence`: find passages related to a proposition.
- `GET /api/gpt/citations/{citation_id}`: resolve a citation.
- `GET /api/gpt/documents/{document_id}/pages`: read consecutive OCR pages.
