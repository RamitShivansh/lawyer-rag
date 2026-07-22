from __future__ import annotations

import argparse
import json
from pathlib import Path

from lawyer_rag.config import get_settings
from lawyer_rag.db import session_scope
from lawyer_rag.retrieval import RetrievalService, retrieval_metrics


def load_cases(path: Path) -> list[dict]:
    cases = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            case = json.loads(line)
            required = {"matter_id", "query", "relevant_citation_ids"}
            if not required.issubset(case):
                missing = ", ".join(sorted(required - set(case)))
                raise ValueError(f"Line {line_number} is missing: {missing}")
            cases.append(case)
    return cases


def run(path: Path) -> dict[str, dict[str, float]]:
    settings = get_settings()
    cases = load_cases(path)
    output: dict[str, dict[str, float]] = {}
    with session_scope() as session:
        service = RetrievalService(session, settings)
        for mode in ("lexical", "dense", "hybrid", "reranked"):
            rankings = []
            relevant = []
            for case in cases:
                hits = service.search(
                    case["matter_id"], case["query"], top_k=10, mode=mode
                )
                rankings.append([hit.citation_id for hit in hits])
                relevant.append(set(case["relevant_citation_ids"]))
            output[mode] = retrieval_metrics(rankings, relevant)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Legal RAG retrieval from JSONL")
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.dataset), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
