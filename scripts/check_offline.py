"""Smoke-test core runtime dependencies without making outbound calls."""

from lawyer_rag.config import get_settings
from lawyer_rag.retrieval import SearchIndex


def main() -> None:
    settings = get_settings()
    if not settings.model_local_files_only:
        raise SystemExit("LEGAL_RAG_MODEL_LOCAL_FILES_ONLY must be true")
    index = SearchIndex(settings)
    list(index.dense_model.embed(["offline model probe"]))
    list(index.sparse_model.embed(["offline model probe"]))
    list(index.reranker.rerank("offline", ["offline model probe"]))
    print("All local models loaded without remote inference configuration.")


if __name__ == "__main__":
    main()
