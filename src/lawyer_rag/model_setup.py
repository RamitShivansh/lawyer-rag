from __future__ import annotations

from fastembed import SparseTextEmbedding, TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

from lawyer_rag.config import get_settings


def main() -> None:
    settings = get_settings()
    dense = TextEmbedding(
        model_name=settings.dense_model,
        cache_dir=str(settings.model_cache_dir),
        local_files_only=False,
    )
    sparse = SparseTextEmbedding(
        model_name=settings.sparse_model,
        cache_dir=str(settings.model_cache_dir),
        local_files_only=False,
    )
    reranker = TextCrossEncoder(
        model_name=settings.reranker_model,
        cache_dir=str(settings.model_cache_dir),
        local_files_only=False,
    )
    list(dense.embed(["model readiness probe"]))
    list(sparse.embed(["model readiness probe"]))
    list(reranker.rerank("model readiness", ["model readiness probe"]))


if __name__ == "__main__":
    main()
