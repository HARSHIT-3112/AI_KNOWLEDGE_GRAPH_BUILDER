"""M7: create the vector + full-text indexes and embed every chunk that needs it.

Idempotent: chunks already embedded with the current model are skipped.

Run:  .venv/bin/python -m scripts.m7_embed
"""
import time

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.retrieval.embedder import OllamaEmbedder
from app.retrieval.search import ensure_indexes, embed_missing_chunks

if __name__ == "__main__":
    s = get_settings()
    driver, embedder = get_driver(), OllamaEmbedder()
    try:
        ensure_indexes(driver, s.neo4j_database, embedder.dimensions)
        t0 = time.perf_counter()
        n = embed_missing_chunks(driver, s.neo4j_database, embedder)
        total = driver.execute_query("MATCH (c:Chunk) RETURN count(c) AS all, count(c.embedding) AS embedded",
                                     database_=s.neo4j_database).records[0]
        print(f"Embedded {n} chunk(s) with {embedder.model} in {time.perf_counter() - t0:.1f}s")
        print(f"Chunks: {total['all']} total, {total['embedded']} with embeddings")
    finally:
        embedder.close()
        close_driver()
