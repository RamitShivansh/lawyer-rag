from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from functools import cached_property
from typing import Any, Literal

import structlog
from fastembed import SparseTextEmbedding, TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from qdrant_client import QdrantClient, models
from sqlalchemy import select
from sqlalchemy.orm import Session

from lawyer_rag.config import Settings
from lawyer_rag.models import Chunk, Document, DocumentStatus, Matter, Page
from lawyer_rag.schemas import (
    CitationResult,
    ReadDocumentResult,
    ReadPage,
    SearchHit,
)


logger = structlog.get_logger()
SearchMode = Literal["lexical", "dense", "hybrid", "reranked"]


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("document_id") == right.get("document_id")
        and int(left.get("page_start", 0)) <= int(right.get("page_end", 0))
        and int(right.get("page_start", 0)) <= int(left.get("page_end", 0))
        and abs(int(left.get("sequence", 0)) - int(right.get("sequence", 0))) <= 1
    )


def collapse_overlaps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for item in items:
        if not any(_overlaps(item, existing) for existing in kept):
            kept.append(item)
    return kept


class SearchIndex:
    dense_vector_name = "dense"
    sparse_vector_name = "sparse"

    def __init__(self, settings: Settings, client: QdrantClient | None = None):
        self.settings = settings
        if client is not None:
            self.client = client
        elif settings.qdrant_url == ":memory:":
            self.client = QdrantClient(":memory:")
        else:
            self.client = QdrantClient(url=settings.qdrant_url, timeout=120)

    @cached_property
    def reranker(self) -> TextCrossEncoder:
        return TextCrossEncoder(
            model_name=self.settings.reranker_model,
            cache_dir=str(self.settings.model_cache_dir),
            local_files_only=self.settings.model_local_files_only,
        )

    @cached_property
    def dense_model(self) -> TextEmbedding:
        return TextEmbedding(
            model_name=self.settings.dense_model,
            cache_dir=str(self.settings.model_cache_dir),
            local_files_only=self.settings.model_local_files_only,
        )

    @cached_property
    def sparse_model(self) -> SparseTextEmbedding:
        return SparseTextEmbedding(
            model_name=self.settings.sparse_model,
            cache_dir=str(self.settings.model_cache_dir),
            local_files_only=self.settings.model_local_files_only,
        )

    def health(self) -> bool:
        self.client.get_collections()
        return True

    def ensure_collection(self) -> None:
        if self.client.collection_exists(self.settings.qdrant_collection):
            return
        dense_size = int(list(self.dense_model.embed(["dimension probe"]))[0].shape[0])
        self.client.create_collection(
            collection_name=self.settings.qdrant_collection,
            vectors_config={
                self.dense_vector_name: models.VectorParams(
                    size=dense_size,
                    distance=models.Distance.COSINE,
                    on_disk=True,
                )
            },
            sparse_vectors_config={
                self.sparse_vector_name: models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
            hnsw_config=models.HnswConfigDiff(on_disk=True, full_scan_threshold=10_000),
        )
        for field in ("matter_id", "document_id", "document_type", "ready"):
            self.client.create_payload_index(
                collection_name=self.settings.qdrant_collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        try:
            self.client.update_collection_aliases(
                change_aliases_operations=[
                    models.CreateAliasOperation(
                        create_alias=models.CreateAlias(
                            collection_name=self.settings.qdrant_collection,
                            alias_name=self.settings.qdrant_alias,
                        )
                    )
                ]
            )
        except Exception:
            logger.warning("qdrant_alias_creation_skipped", alias=self.settings.qdrant_alias)

    @property
    def query_collection(self) -> str:
        try:
            aliases = self.client.get_aliases().aliases
            if any(alias.alias_name == self.settings.qdrant_alias for alias in aliases):
                return self.settings.qdrant_alias
        except Exception:
            pass
        return self.settings.qdrant_collection

    def index_document(self, document: Document, chunks: list[Chunk], *, ready: bool) -> None:
        self.ensure_collection()
        texts = [chunk.text for chunk in chunks]
        dense_vectors = list(self.dense_model.passage_embed(texts))
        sparse_vectors = list(self.sparse_model.embed(texts))
        points = [
            models.PointStruct(
                id=chunk.id,
                vector={
                    self.dense_vector_name: dense_vector.tolist(),
                    self.sparse_vector_name: models.SparseVector(
                        indices=sparse_vector.indices.tolist(),
                        values=sparse_vector.values.tolist(),
                    ),
                },
                payload={
                    "chunk_id": chunk.id,
                    "citation_id": chunk.citation_id,
                    "matter_id": chunk.matter_id,
                    "document_id": document.id,
                    "document_name": document.original_name,
                    "document_type": document.document_type,
                    "sequence": chunk.sequence,
                    "text": chunk.text,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "ready": str(ready).lower(),
                },
            )
            for chunk, dense_vector, sparse_vector in zip(
                chunks, dense_vectors, sparse_vectors, strict=True
            )
        ]
        self.client.upload_points(
            collection_name=self.settings.qdrant_collection,
            points=points,
            batch_size=64,
            wait=True,
        )

    def _document_filter(self, document_id: str) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                )
            ]
        )

    def mark_document_ready(self, document_id: str) -> None:
        self.client.set_payload(
            collection_name=self.settings.qdrant_collection,
            payload={"ready": "true"},
            points=self._document_filter(document_id),
            wait=True,
        )

    def delete_document(self, document_id: str) -> None:
        if not self.client.collection_exists(self.settings.qdrant_collection):
            return
        self.client.delete(
            collection_name=self.settings.qdrant_collection,
            points_selector=models.FilterSelector(filter=self._document_filter(document_id)),
            wait=True,
        )

    @staticmethod
    def _filter(
        matter_id: str,
        document_ids: list[str] | None,
        document_types: list[str] | None,
    ) -> models.Filter:
        conditions: list[models.Condition] = [
            models.FieldCondition(key="matter_id", match=models.MatchValue(value=matter_id)),
            models.FieldCondition(key="ready", match=models.MatchValue(value="true")),
        ]
        if document_ids:
            conditions.append(
                models.FieldCondition(key="document_id", match=models.MatchAny(any=document_ids))
            )
        if document_types:
            conditions.append(
                models.FieldCondition(
                    key="document_type", match=models.MatchAny(any=document_types)
                )
            )
        return models.Filter(must=conditions)

    def query(
        self,
        *,
        query: str,
        matter_id: str,
        document_ids: list[str] | None = None,
        document_types: list[str] | None = None,
        limit: int = 40,
        mode: SearchMode = "hybrid",
    ) -> list[dict[str, Any]]:
        self.ensure_collection()
        query_filter = self._filter(matter_id, document_ids, document_types)
        dense_query = list(self.dense_model.query_embed(query))[0].tolist()
        sparse_embedding = list(self.sparse_model.query_embed(query))[0]
        sparse_query = models.SparseVector(
            indices=sparse_embedding.indices.tolist(),
            values=sparse_embedding.values.tolist(),
        )
        if mode == "lexical":
            response = self.client.query_points(
                collection_name=self.query_collection,
                query=sparse_query,
                using=self.sparse_vector_name,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            retrieval = ["bm25"]
        elif mode == "dense":
            response = self.client.query_points(
                collection_name=self.query_collection,
                query=dense_query,
                using=self.dense_vector_name,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            retrieval = ["dense"]
        else:
            candidate_limit = self.settings.retrieval_candidate_limit
            response = self.client.query_points(
                collection_name=self.query_collection,
                prefetch=[
                    models.Prefetch(
                        query=sparse_query,
                        using=self.sparse_vector_name,
                        filter=query_filter,
                        limit=candidate_limit,
                    ),
                    models.Prefetch(
                        query=dense_query,
                        using=self.dense_vector_name,
                        filter=query_filter,
                        limit=candidate_limit,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=max(limit, self.settings.rerank_limit),
                with_payload=True,
            )
            retrieval = ["bm25", "dense", "rrf"]

        items = [
            {
                "id": str(point.id),
                "score": float(point.score),
                "payload": dict(point.payload or {}),
                "retrieval": retrieval.copy(),
            }
            for point in response.points
        ]
        items = collapse_overlaps(
            [{**item, **item["payload"]} for item in items]
        )
        if mode == "reranked":
            items = self.rerank(query, items)
        return items[:limit]

    def rerank(self, query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = items[: self.settings.rerank_limit]
        if not candidates:
            return []
        try:
            scores = list(
                self.reranker.rerank(query, [str(item.get("text", "")) for item in candidates])
            )
            ranked = sorted(
                zip(candidates, scores, strict=True), key=lambda pair: pair[1], reverse=True
            )
            result = []
            for item, score in ranked:
                updated = dict(item)
                updated["score"] = float(score)
                updated["retrieval"] = [*item.get("retrieval", []), "cross-encoder"]
                result.append(updated)
            return result
        except Exception:
            logger.exception("reranker_failed")
            return candidates


class RetrievalService:
    def __init__(self, session: Session, settings: Settings, index: SearchIndex | None = None):
        self.session = session
        self.settings = settings
        self.index = index or SearchIndex(settings)

    def _require_matter(self, matter_id: str) -> Matter:
        matter = self.session.get(Matter, matter_id)
        if matter is None:
            raise ValueError("Matter not found")
        return matter

    def search(
        self,
        matter_id: str,
        query: str,
        *,
        document_ids: list[str] | None = None,
        document_types: list[str] | None = None,
        top_k: int = 10,
        mode: SearchMode = "reranked",
    ) -> list[SearchHit]:
        self._require_matter(matter_id)
        cleaned = " ".join(query.split())
        if not cleaned:
            raise ValueError("Query cannot be empty")
        limit = min(max(top_k, 1), self.settings.max_result_limit)
        items = self.index.query(
            query=cleaned,
            matter_id=matter_id,
            document_ids=document_ids,
            document_types=document_types,
            limit=limit,
            mode=mode,
        )
        ready_document_ids = set(
            self.session.scalars(
                select(Document.id).where(
                    Document.matter_id == matter_id,
                    Document.status == DocumentStatus.READY.value,
                )
            )
        )
        hits: list[SearchHit] = []
        for item in items:
            if str(item.get("document_id")) not in ready_document_ids:
                continue
            warning = self.session.scalar(
                select(Page.quality_warning).where(
                    Page.document_id == str(item["document_id"]),
                    Page.page_number == int(item["page_start"]),
                )
            )
            citation_id = str(item["citation_id"])
            hits.append(
                SearchHit(
                    citation_id=citation_id,
                    document_id=str(item["document_id"]),
                    document_name=str(item["document_name"]),
                    document_type=str(item.get("document_type", "unknown")),
                    excerpt=str(item.get("text", "")),
                    page_start=int(item["page_start"]),
                    page_end=int(item["page_end"]),
                    score=float(item["score"]),
                    source_url=f"{self.settings.base_url}/admin/citations/{citation_id}",
                    ocr_warning=warning,
                    retrieval=list(item.get("retrieval", [])),
                )
            )
        return hits

    def find_evidence(
        self,
        matter_id: str,
        proposition: str,
        *,
        query_variants: list[str] | None = None,
        top_k: int = 15,
    ) -> list[SearchHit]:
        queries = []
        for candidate in [proposition, *(query_variants or [])]:
            cleaned = " ".join(candidate.split())
            if cleaned and cleaned not in queries:
                queries.append(cleaned)
        queries = queries[:5]
        if not queries:
            raise ValueError("Proposition cannot be empty")

        rankings: list[list[str]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for query in queries:
            items = self.index.query(
                query=query,
                matter_id=matter_id,
                limit=self.settings.retrieval_candidate_limit,
                mode="hybrid",
            )
            rankings.append([str(item["id"]) for item in items])
            by_id.update({str(item["id"]): item for item in items})
        fused = reciprocal_rank_fusion(rankings)
        candidates = []
        for item_id, score in fused[: self.settings.rerank_limit]:
            item = dict(by_id[item_id])
            item["score"] = score
            item["retrieval"] = [*item.get("retrieval", []), "query-variant-rrf"]
            candidates.append(item)
        reranked = self.index.rerank(proposition, candidates)

        # Reuse normal result validation and serialization without issuing another vector query.
        limit = min(max(top_k, 1), self.settings.max_result_limit)
        ready_ids = set(
            self.session.scalars(
                select(Document.id).where(
                    Document.matter_id == matter_id,
                    Document.status == DocumentStatus.READY.value,
                )
            )
        )
        result: list[SearchHit] = []
        for item in reranked[:limit]:
            if str(item.get("document_id")) not in ready_ids:
                continue
            citation_id = str(item["citation_id"])
            result.append(
                SearchHit(
                    citation_id=citation_id,
                    document_id=str(item["document_id"]),
                    document_name=str(item["document_name"]),
                    document_type=str(item.get("document_type", "unknown")),
                    excerpt=str(item.get("text", "")),
                    page_start=int(item["page_start"]),
                    page_end=int(item["page_end"]),
                    score=float(item["score"]),
                    source_url=f"{self.settings.base_url}/admin/citations/{citation_id}",
                    retrieval=list(item.get("retrieval", [])),
                )
            )
        return result


def get_citation(session: Session, settings: Settings, citation_id: str) -> CitationResult:
    chunk = session.scalar(select(Chunk).where(Chunk.citation_id == citation_id))
    if chunk is None or chunk.document.status != DocumentStatus.READY.value:
        raise ValueError("Citation not found")
    warnings = list(
        session.scalars(
            select(Page.quality_warning).where(
                Page.document_id == chunk.document_id,
                Page.page_number.between(chunk.page_start, chunk.page_end),
                Page.quality_warning.is_not(None),
            )
        )
    )
    return CitationResult(
        citation_id=chunk.citation_id,
        document_id=chunk.document_id,
        document_name=chunk.document.original_name,
        matter_id=chunk.matter_id,
        excerpt=chunk.text,
        context=chunk.parent_text,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        coordinates=chunk.coordinates,
        source_url=f"{settings.base_url}/admin/citations/{chunk.citation_id}",
        original_url=f"{settings.base_url}/admin/documents/{chunk.document_id}/original",
        ocr_warnings=[warning for warning in warnings if warning],
    )


def read_document(
    session: Session,
    settings: Settings,
    document_id: str,
    start_page: int,
    page_count: int,
) -> ReadDocumentResult:
    document = session.get(Document, document_id)
    if document is None or document.status != DocumentStatus.READY.value:
        raise ValueError("Ready document not found")
    if start_page < 1:
        raise ValueError("start_page must be at least 1")
    count = min(max(page_count, 1), settings.read_page_limit)
    pages = list(
        session.scalars(
            select(Page)
            .where(
                Page.document_id == document_id,
                Page.page_number >= start_page,
                Page.page_number < start_page + count,
            )
            .order_by(Page.page_number)
        )
    )
    last_page = pages[-1].page_number if pages else start_page - 1
    complete = last_page >= document.page_count
    return ReadDocumentResult(
        document_id=document.id,
        document_name=document.original_name,
        start_page=start_page,
        pages=[
            ReadPage(
                page_number=page.page_number,
                text=page.text,
                quality_warning=page.quality_warning,
            )
            for page in pages
        ],
        next_start_page=None if complete else last_page + 1,
        complete=complete,
    )


def retrieval_metrics(rankings: Iterable[list[str]], relevant: Iterable[set[str]]) -> dict[str, float]:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for ranking, relevant_ids in zip(rankings, relevant, strict=True):
        top = ranking[:10]
        if not relevant_ids:
            continue
        hits = [1 if item in relevant_ids else 0 for item in top]
        recalls.append(sum(hits) / len(relevant_ids))
        reciprocal_ranks.append(
            next((1.0 / rank for rank, hit in enumerate(hits, start=1) if hit), 0.0)
        )
        dcg = sum(hit / math.log2(rank + 1) for rank, hit in enumerate(hits, start=1))
        ideal_hits = [1] * min(len(relevant_ids), 10)
        ideal_dcg = sum(
            hit / math.log2(rank + 1) for rank, hit in enumerate(ideal_hits, start=1)
        )
        ndcgs.append(dcg / ideal_dcg if ideal_dcg else 0.0)
    count = len(recalls)
    return {
        "queries": float(count),
        "recall_at_10": sum(recalls) / count if count else 0.0,
        "mrr_at_10": sum(reciprocal_ranks) / count if count else 0.0,
        "ndcg_at_10": sum(ndcgs) / count if count else 0.0,
    }
