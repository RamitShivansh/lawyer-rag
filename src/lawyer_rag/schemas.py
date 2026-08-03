from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    matter_id: str
    query: str
    document_ids: list[str] | None = None
    document_types: list[str] | None = None
    top_k: int = Field(default=10, ge=1, le=20)


class FindEvidenceRequest(BaseModel):
    matter_id: str
    proposition: str
    query_variants: list[str] | None = None
    top_k: int = Field(default=15, ge=1, le=20)


class MatterResult(BaseModel):
    id: str
    name: str


class DocumentResult(BaseModel):
    id: str
    matter_id: str
    name: str
    document_type: str
    status: str
    page_count: int
    processed_pages: int
    error: str | None = None


class SearchHit(BaseModel):
    citation_id: str
    document_id: str
    document_name: str
    document_type: str
    excerpt: str
    page_start: int
    page_end: int
    score: float
    source_url: str
    ocr_warning: str | None = None
    retrieval: list[str] = Field(default_factory=list)


class CitationResult(BaseModel):
    citation_id: str
    document_id: str
    document_name: str
    matter_id: str
    excerpt: str
    context: str
    page_start: int
    page_end: int
    coordinates: dict[str, Any]
    source_url: str
    original_url: str
    ocr_warnings: list[str]


class ReadPage(BaseModel):
    page_number: int
    text: str
    quality_warning: str | None


class ReadDocumentResult(BaseModel):
    document_id: str
    document_name: str
    start_page: int
    pages: list[ReadPage]
    next_start_page: int | None
    complete: bool


class AppSearchResult(BaseModel):
    id: str
    title: str
    url: str


class AppSearchOutput(BaseModel):
    results: list[AppSearchResult]


class AppFetchOutput(BaseModel):
    id: str
    title: str
    text: str
    url: str
    metadata: dict[str, Any] | None = None
