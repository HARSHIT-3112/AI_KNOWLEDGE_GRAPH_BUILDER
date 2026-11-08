"""M7: measure retrieval instead of eyeballing it (guide §12.1: Recall@k, MRR).

For each labelled question, a retrieved chunk is "relevant" if it comes from
the labelled document AND one of the labelled pages.

  Recall@k (hit rate) : share of questions with at least one relevant chunk in the top k
  MRR                 : mean of 1/rank of the FIRST relevant chunk (1.0 = always first)

Run:  .venv/bin/python -m scripts.m7_eval [--k 5] [--verbose]
"""
import argparse
import json
from pathlib import Path

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.retrieval.embedder import OllamaEmbedder
from app.retrieval.search import hybrid_search, keyword_search, vector_search


def first_relevant_rank(rows: list[dict], document: str, pages: set[int]) -> int | None:
    for rank, r in enumerate(rows, start=1):
        if r["document"] == document and r["page"] in pages:
            return rank
    return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--file", default="eval/retrieval_guide.json")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--gate", choices=["keyword", "vector", "hybrid"], help="method the thresholds apply to")
    ap.add_argument("--min-recall", type=float, default=0.0)
    ap.add_argument("--min-mrr", type=float, default=0.0)
    args = ap.parse_args()

    spec = json.loads(Path(args.file).read_text())
    db = get_settings().neo4j_database
    driver, embedder = get_driver(), OllamaEmbedder()
    methods = {
        "keyword": lambda q: keyword_search(driver, db, q, args.k),
        "vector": lambda q: vector_search(driver, db, embedder, q, args.k),
        "hybrid": lambda q: hybrid_search(driver, db, embedder, q, args.k),
    }
    ranks: dict[str, list[int | None]] = {m: [] for m in methods}
    try:
        for item in spec["questions"]:
            pages = set(item["relevant_pages"])
            line = []
            for name, fn in methods.items():
                rank = first_relevant_rank(fn(item["q"]), spec["document"], pages)
                ranks[name].append(rank)
                line.append(f"{name}={rank or '-'}")
            if args.verbose:
                print(f"{'  '.join(f'{x:<10}' for x in line)}  {item['q']}")
    finally:
        embedder.close()
        close_driver()

    n = len(spec["questions"])
    print(f"\n{n} questions, top-{args.k}\n")
    print(f"{'method':<8} {'Recall@' + str(args.k):>9} {'MRR':>6}")
    scores = {}
    for name, rs in ranks.items():
        recall = sum(r is not None for r in rs) / n
        mrr = sum(1 / r for r in rs if r) / n
        scores[name] = (recall, mrr)
        print(f"{name:<8} {recall:>9.2f} {mrr:>6.2f}")

    # M10 regression gate: a change that makes retrieval worse fails the build.
    if args.gate:
        recall, mrr = scores[args.gate]
        if recall < args.min_recall or mrr < args.min_mrr:
            raise SystemExit(f"\nGATE FAILED: {args.gate} recall {recall:.2f} (min {args.min_recall}), "
                             f"MRR {mrr:.2f} (min {args.min_mrr})")
        print(f"\ngate passed: {args.gate} recall >= {args.min_recall}, MRR >= {args.min_mrr}")
