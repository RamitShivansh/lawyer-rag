from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lawyer_rag.chunking import build_chunks
from lawyer_rag.config import Settings
from lawyer_rag.models import (
    Chunk,
    Document,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    Page,
)
from lawyer_rag.ocr import extract_pdf, run_ocr
from lawyer_rag.storage import Storage


logger = structlog.get_logger()


def claim_job(session: Session) -> IngestionJob | None:
    job = session.scalar(
        select(IngestionJob)
        .where(IngestionJob.status == JobStatus.QUEUED.value)
        .order_by(IngestionJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.status = JobStatus.RUNNING.value
    job.started_at = datetime.now(UTC)
    job.attempts += 1
    job.error_message = None
    job.document.status = DocumentStatus.PROCESSING.value
    job.document.error_message = None
    session.commit()
    session.refresh(job)
    return job


def _progress(job_id: str, document_id: str, page: int) -> None:
    from lawyer_rag.db import session_scope

    with session_scope() as session:
        job = session.get(IngestionJob, job_id)
        document = session.get(Document, document_id)
        if job:
            job.progress_page = page
        if document:
            document.processed_pages = page


def process_job(job_id: str, settings: Settings) -> None:
    from lawyer_rag.db import session_scope
    from lawyer_rag.retrieval import SearchIndex

    storage = Storage(settings)
    document_id = ""
    matter_id = ""
    try:
        with session_scope() as session:
            job = session.get(IngestionJob, job_id)
            if job is None:
                raise ValueError("Ingestion job not found")
            document = job.document
            document_id = document.id
            matter_id = document.matter_id
            original = storage.original_path(matter_id, document_id)
            ocr = storage.ocr_path(matter_id, document_id)
            sidecar = storage.sidecar_path(matter_id, document_id)

        logger.info("ingestion_ocr_started", job_id=job_id, document_id=document_id)
        run_ocr(original, ocr, sidecar, settings)
        pages, blocks = extract_pdf(
            ocr, progress=lambda page: _progress(job_id, document_id, page)
        )
        drafts = build_chunks(blocks)
        if not drafts:
            raise RuntimeError("OCR completed but produced no searchable printed text")

        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError("Document disappeared during ingestion")
            session.execute(delete(Page).where(Page.document_id == document_id))
            session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            session.flush()
            session.add_all(
                [
                    Page(
                        document_id=document_id,
                        page_number=page.page_number,
                        text=page.text,
                        width=page.width,
                        height=page.height,
                        quality_score=page.quality_score,
                        quality_warning=page.quality_warning,
                        words=page.words,
                    )
                    for page in pages
                ]
            )
            chunks = [
                Chunk(
                    citation_id=f"cite:{document_id}:{draft.sequence}:{settings.parser_version}",
                    document_id=document_id,
                    matter_id=matter_id,
                    sequence=draft.sequence,
                    text=draft.text,
                    parent_text=draft.parent_text,
                    page_start=draft.page_start,
                    page_end=draft.page_end,
                    paragraph_label=draft.paragraph_label,
                    coordinates=draft.coordinates,
                    token_count=draft.token_count,
                    parser_version=settings.parser_version,
                )
                for draft in drafts
            ]
            session.add_all(chunks)
            document.ocr_path = str(ocr)
            document.parser_version = settings.parser_version
            document.index_version = settings.qdrant_collection
            document.processed_pages = len(pages)
            session.flush()
            chunk_ids = [chunk.id for chunk in chunks]

        index = SearchIndex(settings)
        index.delete_document(document_id)
        with session_scope() as session:
            chunks = list(
                session.scalars(
                    select(Chunk).where(Chunk.id.in_(chunk_ids)).order_by(Chunk.sequence)
                )
            )
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError("Document disappeared before indexing")
            index.index_document(document, chunks, ready=False)

        with session_scope() as session:
            job = session.get(IngestionJob, job_id)
            document = session.get(Document, document_id)
            if job is None or document is None:
                raise ValueError("Ingestion state disappeared")
            document.status = DocumentStatus.READY.value
            document.error_message = None
            job.status = JobStatus.SUCCEEDED.value
            job.finished_at = datetime.now(UTC)
            job.progress_page = document.page_count

        index.mark_document_ready(document_id)
        logger.info(
            "ingestion_succeeded",
            job_id=job_id,
            document_id=document_id,
            page_count=len(pages),
            chunk_count=len(drafts),
        )
    except Exception as exc:
        logger.exception("ingestion_failed", job_id=job_id, document_id=document_id)
        try:
            from lawyer_rag.retrieval import SearchIndex

            if document_id:
                SearchIndex(settings).delete_document(document_id)
        except Exception:
            logger.exception("ingestion_index_cleanup_failed", document_id=document_id)
        with session_scope() as session:
            job = session.get(IngestionJob, job_id)
            if job:
                job.status = JobStatus.FAILED.value
                job.finished_at = datetime.now(UTC)
                job.error_message = str(exc)[:2000]
                document = job.document
                document.status = DocumentStatus.FAILED.value
                document.error_message = str(exc)[:2000]
                session.execute(delete(Page).where(Page.document_id == document.id))
                session.execute(delete(Chunk).where(Chunk.document_id == document.id))
        raise
