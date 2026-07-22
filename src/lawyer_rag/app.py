from __future__ import annotations

import contextlib
import os
from pathlib import Path
from urllib.parse import quote

import structlog
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from lawyer_rag.config import get_settings
from lawyer_rag.db import create_schema, engine, session_scope
from lawyer_rag.documents import (
    DuplicateDocumentError,
    archive_document,
    create_matter,
    retry_document,
    upload_document,
)
from lawyer_rag.logging_config import configure_logging
from lawyer_rag.mcp_server import mcp
from lawyer_rag.models import Document, Matter
from lawyer_rag.ocr import PDFValidationError
from lawyer_rag.retrieval import RetrievalService, SearchIndex, get_citation, read_document
from lawyer_rag.schemas import (
    CitationResult,
    DocumentResult,
    FindEvidenceRequest,
    MatterResult,
    ReadDocumentResult,
    ReadPage,
    SearchHit,
    SearchRequest,
)
from lawyer_rag.security import (
    MCPAuthMiddleware,
    constant_time_equal,
    ensure_csrf,
    require_admin,
    require_bearer_token,
    verify_csrf,
)
from lawyer_rag.storage import Storage

settings = get_settings()
configure_logging(settings.log_level)
logger = structlog.get_logger()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
UPLOAD_FILES = File(...)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    create_schema()
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Legal Case File RAG",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="strict",
    https_only=settings.secure_cookies,
    max_age=8 * 60 * 60,
)


def _flash(request: Request, message: str, level: str = "info") -> None:
    request.session["flash"] = {"message": message, "level": level}


def _context(request: Request, **values):
    return {
        "request": request,
        "csrf": ensure_csrf(request),
        "flash": request.session.pop("flash", None),
        **values,
    }


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/admin/") and not value.startswith("//"):
        return value
    return "/admin"


def _require_gpt_action(request: Request) -> None:
    require_bearer_token(request.headers.get("authorization", ""), settings.mcp_token)


def _gpt_openapi_schema() -> dict:
    server_url = settings.base_url.rstrip("/")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Legal Case File RAG GPT Actions",
            "version": "0.1.0",
            "description": (
                "Read-only action API for searching uploaded legal case files, resolving "
                "citations, and reading OCR pages."
            ),
        },
        "servers": [{"url": server_url}],
        "paths": {
            "/api/gpt/matters": {
                "get": {
                    "operationId": "listMatters",
                    "summary": "List case matters",
                    "description": "List the matters available in this deployment.",
                    "security": [{"BearerAuth": []}],
                    "responses": {
                        "200": {
                            "description": "Matter list",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/MatterResult"},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/gpt/matters/{matter_id}/documents": {
                "get": {
                    "operationId": "listDocuments",
                    "summary": "List documents for one matter",
                    "security": [{"BearerAuth": []}],
                    "parameters": [
                        {
                            "name": "matter_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Document list",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/DocumentResult"},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/gpt/search": {
                "post": {
                    "operationId": "searchCaseFile",
                    "summary": "Search a matter for citation-ready evidence",
                    "security": [{"BearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SearchRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Search hits",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/SearchHit"},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/gpt/find-evidence": {
                "post": {
                    "operationId": "findEvidence",
                    "summary": "Find passages related to a proposition",
                    "security": [{"BearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/FindEvidenceRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Evidence hits",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/SearchHit"},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/gpt/citations/{citation_id}": {
                "get": {
                    "operationId": "getCitation",
                    "summary": "Resolve a citation",
                    "security": [{"BearerAuth": []}],
                    "parameters": [
                        {
                            "name": "citation_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Citation details",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CitationResult"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/gpt/documents/{document_id}/pages": {
                "get": {
                    "operationId": "readDocument",
                    "summary": "Read consecutive OCR pages",
                    "security": [{"BearerAuth": []}],
                    "parameters": [
                        {
                            "name": "document_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "start_page",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "default": 1},
                        },
                        {
                            "name": "page_count",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": settings.read_page_limit,
                                "default": settings.read_page_limit,
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "OCR page batch",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ReadDocumentResult"
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                model.__name__: model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
                for model in (
                    SearchRequest,
                    FindEvidenceRequest,
                    MatterResult,
                    DocumentResult,
                    SearchHit,
                    CitationResult,
                    ReadPage,
                    ReadDocumentResult,
                )
            },
        },
    }


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/health/ready")
def ready() -> JSONResponse:
    checks: dict[str, bool] = {
        "postgres": False,
        "qdrant": False,
        "storage": False,
        "models": False,
    }
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:
        logger.exception("readiness_database_failed")
    try:
        checks["qdrant"] = SearchIndex(settings).health()
    except Exception:
        logger.exception("readiness_qdrant_failed")
    checks["storage"] = os.access(settings.data_dir, os.W_OK)
    checks["models"] = not settings.model_local_files_only or any(
        settings.model_cache_dir.iterdir()
    )
    ok = all(checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks},
        status_code=200 if ok else 503,
    )


@app.get("/gpt/openapi.json", include_in_schema=False)
def gpt_openapi() -> JSONResponse:
    return JSONResponse(_gpt_openapi_schema())


@app.get("/gpt/privacy", response_class=HTMLResponse, include_in_schema=False)
def gpt_privacy() -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Legal Case File RAG GPT Privacy</title></head>
<body>
<main>
<h1>Legal Case File RAG GPT Privacy Policy</h1>
<p>This custom GPT connects to your privately hosted Legal Case File RAG deployment through
read-only API actions. The API can return matter names, document metadata, OCR text excerpts,
citation details, and OCR page text from documents uploaded by your operator.</p>
<p>The GPT action API does not upload, edit, archive, delete, or create case files. It does not
store generated answers. Your hosted application stores uploaded documents, OCR output,
retrieval indexes, and operational logs according to your deployment configuration.</p>
<p>Access is protected with a bearer token configured in the GPT action settings. Only share the
GPT with users who are allowed to access the matters available in this deployment.</p>
<p>This application is a retrieval aid for professional legal work. Outputs should be reviewed
by a qualified person before use.</p>
</main>
</body>
</html>
        """.strip()
    )


@app.get(
    "/api/gpt/matters",
    response_model=list[MatterResult],
    dependencies=[Depends(_require_gpt_action)],
    tags=["GPT Actions"],
)
def gpt_list_matters() -> list[dict]:
    with session_scope() as session:
        matters = list(session.scalars(select(Matter).order_by(Matter.created_at)))
        return [MatterResult(id=matter.id, name=matter.name).model_dump() for matter in matters]


@app.get(
    "/api/gpt/matters/{matter_id}/documents",
    response_model=list[DocumentResult],
    dependencies=[Depends(_require_gpt_action)],
    tags=["GPT Actions"],
)
def gpt_list_documents(matter_id: str) -> list[dict]:
    with session_scope() as session:
        if session.get(Matter, matter_id) is None:
            raise HTTPException(status_code=404, detail="Matter not found")
        documents = list(
            session.scalars(
                select(Document)
                .where(Document.matter_id == matter_id)
                .order_by(Document.created_at)
            )
        )
        return [
            DocumentResult(
                id=document.id,
                matter_id=document.matter_id,
                name=document.original_name,
                document_type=document.document_type,
                status=document.status,
                page_count=document.page_count,
                processed_pages=document.processed_pages,
                error=document.error_message,
            ).model_dump()
            for document in documents
        ]


@app.post(
    "/api/gpt/search",
    response_model=list[SearchHit],
    dependencies=[Depends(_require_gpt_action)],
    tags=["GPT Actions"],
)
def gpt_search_case_file(payload: SearchRequest) -> list[dict]:
    with session_scope() as session:
        hits = RetrievalService(session, settings).search(
            payload.matter_id,
            payload.query,
            document_ids=payload.document_ids,
            document_types=payload.document_types,
            top_k=payload.top_k,
        )
        return [hit.model_dump(mode="json") for hit in hits]


@app.post(
    "/api/gpt/find-evidence",
    response_model=list[SearchHit],
    dependencies=[Depends(_require_gpt_action)],
    tags=["GPT Actions"],
)
def gpt_find_evidence(payload: FindEvidenceRequest) -> list[dict]:
    with session_scope() as session:
        hits = RetrievalService(session, settings).find_evidence(
            payload.matter_id,
            payload.proposition,
            query_variants=payload.query_variants,
            top_k=payload.top_k,
        )
        return [hit.model_dump(mode="json") for hit in hits]


@app.get(
    "/api/gpt/citations/{citation_id}",
    response_model=CitationResult,
    dependencies=[Depends(_require_gpt_action)],
    tags=["GPT Actions"],
)
def gpt_get_citation(citation_id: str) -> dict:
    with session_scope() as session:
        return get_citation(session, settings, citation_id).model_dump(mode="json")


@app.get(
    "/api/gpt/documents/{document_id}/pages",
    response_model=ReadDocumentResult,
    dependencies=[Depends(_require_gpt_action)],
    tags=["GPT Actions"],
)
def gpt_read_document(
    document_id: str, start_page: int = 1, page_count: int = settings.read_page_limit
) -> dict:
    with session_scope() as session:
        return read_document(session, settings, document_id, start_page, page_count).model_dump(
            mode="json"
        )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_context(request, next=_safe_next(next), error=None),
    )


@app.post("/admin/login", response_class=HTMLResponse)
def login(request: Request, token: str = Form(...), next: str = Form("/admin")):
    if not constant_time_equal(token, settings.admin_token):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_context(request, next=_safe_next(next), error="Invalid admin token"),
            status_code=401,
        )
    request.session.clear()
    request.session["admin"] = True
    ensure_csrf(request)
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/admin/logout")
def logout(request: Request, csrf: str = Form(...)):
    require_admin(request)
    verify_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request):
    if request.session.get("admin") is not True:
        return RedirectResponse("/admin/login", status_code=303)
    with session_scope() as session:
        matters = list(session.scalars(select(Matter).order_by(Matter.created_at.desc())))
        documents = list(session.scalars(select(Document).order_by(Document.created_at.desc())))
        docs_by_matter: dict[str, list[Document]] = {matter.id: [] for matter in matters}
        for document in documents:
            docs_by_matter.setdefault(document.matter_id, []).append(document)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_context(request, matters=matters, docs_by_matter=docs_by_matter),
        )


@app.post("/admin/matters")
def add_matter(request: Request, name: str = Form(...), csrf: str = Form(...)):
    require_admin(request)
    verify_csrf(request, csrf)
    try:
        with session_scope() as session:
            matter = create_matter(session, name)
        _flash(request, f"Created matter: {matter.name}", "success")
    except ValueError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/matters/{matter_id}/documents")
def add_documents(
    request: Request,
    matter_id: str,
    files: list[UploadFile] = UPLOAD_FILES,
    csrf: str = Form(...),
    document_type: str = Form("unknown"),
):
    require_admin(request)
    verify_csrf(request, csrf)
    successes = 0
    errors: list[str] = []
    for uploaded in files:
        try:
            with session_scope() as session:
                upload_document(
                    session,
                    settings,
                    matter_id=matter_id,
                    original_name=uploaded.filename or "document.pdf",
                    document_type=document_type,
                    stream=uploaded.file,
                )
            successes += 1
        except (ValueError, PDFValidationError, DuplicateDocumentError) as exc:
            errors.append(f"{uploaded.filename or 'document'}: {exc}")
        finally:
            uploaded.file.close()
    if errors:
        _flash(request, f"Queued {successes}. " + " | ".join(errors), "error")
    else:
        _flash(request, f"Queued {successes} document(s) for OCR", "success")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/documents/{document_id}/retry")
def retry(request: Request, document_id: str, csrf: str = Form(...)):
    require_admin(request)
    verify_csrf(request, csrf)
    try:
        with session_scope() as session:
            retry_document(session, document_id)
        _flash(request, "Document queued for retry", "success")
    except ValueError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/documents/{document_id}/archive")
def archive(request: Request, document_id: str, csrf: str = Form(...)):
    require_admin(request)
    verify_csrf(request, csrf)
    try:
        with session_scope() as session:
            document = archive_document(session, document_id)
        SearchIndex(settings).delete_document(document.id)
        _flash(request, "Document archived and removed from retrieval", "success")
    except ValueError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/citations/{citation_id}", response_class=HTMLResponse)
def citation_view(request: Request, citation_id: str, page: int | None = None):
    if request.session.get("admin") is not True:
        next_url = quote(str(request.url.path), safe="/")
        return RedirectResponse(f"/admin/login?next={next_url}", status_code=303)
    try:
        with session_scope() as session:
            citation = get_citation(session, settings, citation_id)
        selected_page = page or citation.page_start
        page_data = citation.coordinates.get("pages", {}).get(str(selected_page), {})
        return templates.TemplateResponse(
            request=request,
            name="citation.html",
            context=_context(
                request,
                citation=citation,
                selected_page=selected_page,
                page_data=page_data,
            ),
        )
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=404)


@app.get("/admin/documents/{document_id}/pages/{page}/image")
def page_image(request: Request, document_id: str, page: int):
    require_admin(request)
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None or page < 1 or page > document.page_count:
            return JSONResponse({"detail": "Page not found"}, status_code=404)
        path = Storage(settings).render_preview(document.matter_id, document.id, page)
    return FileResponse(path, media_type="image/png")


@app.get("/admin/documents/{document_id}/original")
def original_pdf(request: Request, document_id: str):
    require_admin(request)
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            return JSONResponse({"detail": "Document not found"}, status_code=404)
        path = Storage(settings).original_path(document.matter_id, document.id)
        return FileResponse(path, media_type="application/pdf", filename=document.original_name)


mcp_app = MCPAuthMiddleware(mcp.streamable_http_app(), settings)
app.mount("/mcp", mcp_app)
