from __future__ import annotations

import contextlib
import os
from pathlib import Path
from urllib.parse import quote

import structlog
from fastapi import FastAPI, File, Form, Request, UploadFile
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
from lawyer_rag.retrieval import SearchIndex, get_citation
from lawyer_rag.security import (
    MCPAuthMiddleware,
    constant_time_equal,
    ensure_csrf,
    require_admin,
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


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
def oauth_protected_resource_metadata() -> dict[str, object]:
    return {
        "resource": settings.oauth_resource,
        "authorization_servers": [settings.oauth_authorization_server],
        "scopes_supported": settings.oauth_scope_list,
    }


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
