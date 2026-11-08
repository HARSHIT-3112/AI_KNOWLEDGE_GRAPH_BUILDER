"""M8: ask the knowledge base a question — GraphRAG with citations and abstention.

Needs Neo4j (Docker) + Ollama. On an 8 GB Mac expect ~30–90 s per answer.

Run:
  .venv/bin/python -m scripts.m8_ask "Which project does Harshit work on, and who else is on it?"
  .venv/bin/python -m scripts.m8_ask "What is the capital of France?"
  .venv/bin/python -m scripts.m8_ask "Why use MERGE instead of CREATE?" --show-context
"""
import argparse

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.extraction.llm_client import OllamaClient
from app.retrieval.answerer import answer_question
from app.retrieval.embedder import OllamaEmbedder


def print_result(r) -> None:
    print(f"\nQ: {r.question}")
    linked = ", ".join(f"{e.name} ({e.type})" for e in r.linked_entities) or "none"
    print(f"   linked entities: {linked} | top vector score: {r.top_vector_score:.3f}")
    print(f"\nA: {r.answer}")
    if r.grounded:
        print("\nSources:")
        for c in r.citations:
            print(f"   [{c.id}] {c.kind}: {c.document} p{c.page} — {c.text[:100].replace(chr(10), ' ')}")
        if r.invalid_citations:
            print(f"   ⚠ model also cited non-existent sources: {r.invalid_citations}")
        if r.unsupported_terms:
            print(f"   ⚠ not found in the cited sources (check by hand): {r.unsupported_terms}")
    else:
        print(f"   (abstained: {r.abstained_reason})")
    if r.llm_seconds:
        print(f"   [LLM {r.llm_seconds:.0f}s]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("questions", nargs="+")
    ap.add_argument("--show-context", action="store_true")
    args = ap.parse_args()

    db = get_settings().neo4j_database
    driver, embedder, llm = get_driver(), OllamaEmbedder(), OllamaClient()
    try:
        llm.ensure_model()
        for q in args.questions:
            r = answer_question(driver, db, q, embedder, llm)
            if args.show_context and r.context:
                print("\n----- context sent to the LLM -----\n" + r.context + "\n-----------------------------------")
            print_result(r)
    finally:
        embedder.close()
        llm.close()
        close_driver()
