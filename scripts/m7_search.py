"""M7: ask a question, see which chunks keyword search and vector search return.

Run:
  .venv/bin/python -m scripts.m7_search "how do I avoid duplicate nodes when re-running ingestion?"
  .venv/bin/python -m scripts.m7_search "who builds the document intelligence product?" --k 3
"""
import argparse

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.retrieval.embedder import OllamaEmbedder
from app.retrieval.search import keyword_search, vector_search


def show(title: str, rows: list[dict]) -> None:
    print(f"\n== {title} ==")
    if not rows:
        print("  (no results)")
    for i, r in enumerate(rows, 1):
        snippet = r["text"][:110].replace("\n", " ")
        print(f"  {i}. score {r['score']:.3f}  {r['document']} p{r['page']} #{r['chunk_index']}  | {snippet}...")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    db = get_settings().neo4j_database
    driver, embedder = get_driver(), OllamaEmbedder()
    try:
        show("keyword (full-text)", keyword_search(driver, db, args.question, args.k))
        show("vector (semantic)", vector_search(driver, db, embedder, args.question, args.k))
    finally:
        embedder.close()
        close_driver()
