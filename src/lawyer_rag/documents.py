from __future__ import annotations

import uuid
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lawyer_rag.config import Settings
from lawyer_rag.models import Document, DocumentStatus, IngestionJob, JobStatus, Matter
from lawyer_rag.ocr import PDFValidationError, validate_pdf
from lawyer_rag.storage import Storage


class DuplicateDocumentError(ValueError):
    pass


def create_matter(session: Session, name: str) -> Matter:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > 200:
        raise ValueError("Matter name must contain between 1 and 200 characters")
    matter = Matter(name=cleaned)
    session.add(matter)
    session.commit()
    session.refresh(matter)
    return matter


def upload_document(
    session: Session,
    settings: Settings,
    *,
    matter_id: str,
    original_name: str,
    document_type: str,
    stream: BinaryIO,
) -> Document:
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ValueError("Matter not found")

    document_id = str(uuid.uuid4())
    storage = Storage(settings)
    try:
        path, sha256, size = storage.save_upload(stream, matter_id, document_id)
        page_count = validate_pdf(path, settings)
        duplicate = session.scalar(
            select(Document).where(Document.matter_id == matter_id, Document.sha256 == sha256)
        )
        if duplicate:
            raise DuplicateDocumentError(
                f"This PDF already exists in the matter as {duplicate.original_name}"
            )

        document = Document(
            id=document_id,
            matter_id=matter_id,
            original_name=original_name[:500] or "document.pdf",
            document_type=(document_type.strip() or "unknown")[:100],
            sha256=sha256,
            size_bytes=size,
            page_count=page_count,
            original_path=str(path),
            status=DocumentStatus.UPLOADED.value,
        )
        job = IngestionJob(document=document, status=JobStatus.QUEUED.value)
        session.add_all([document, job])
        session.commit()
        session.refresh(document)
        return document
    except (DuplicateDocumentError, PDFValidationError, ValueError, IntegrityError):
        session.rollback()
        storage.remove_document_dir(matter_id, document_id)
        raise
    except Exception:
        session.rollback()
        storage.remove_document_dir(matter_id, document_id)
        raise


def retry_document(session: Session, document_id: str) -> IngestionJob:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError("Document not found")
    active = session.scalar(
        select(IngestionJob).where(
            IngestionJob.document_id == document_id,
            IngestionJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
        )
    )
    if active:
        return active
    document.status = DocumentStatus.UPLOADED.value
    document.error_message = None
    document.processed_pages = 0
    job = IngestionJob(document_id=document_id, status=JobStatus.QUEUED.value)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def archive_document(session: Session, document_id: str) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError("Document not found")
    document.status = DocumentStatus.ARCHIVED.value
    session.commit()
    return document
