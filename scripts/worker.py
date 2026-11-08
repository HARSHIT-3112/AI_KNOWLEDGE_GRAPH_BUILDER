"""M9: start the background ingestion worker (a separate terminal from the API).

Run:  .venv/bin/python -m scripts.worker            # loop forever
      .venv/bin/python -m scripts.worker --once     # process one job (if any) and exit
"""
import argparse

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.neo4j import close_driver, get_driver
from app.extraction.llm_client import OllamaClient
from app.pipeline.jobs import new_worker_id
from app.pipeline.worker import run_forever, run_one
from app.retrieval.embedder import OllamaEmbedder
from app.retrieval.search import ensure_indexes

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    setup_logging()                       # one JSON object per line (M10)

    db = get_settings().neo4j_database
    driver, llm, embedder = get_driver(), OllamaClient(), OllamaEmbedder()
    try:
        llm.ensure_model()
        ensure_indexes(driver, db, embedder.dimensions)
        if args.once:
            print(run_one(driver, db, new_worker_id(), llm, embedder) or "queue empty")
        else:
            run_forever(driver, db, llm, embedder)
    except KeyboardInterrupt:
        print("\nworker stopped")
    finally:
        llm.close()
        embedder.close()
        close_driver()
