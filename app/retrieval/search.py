"""M7 — Find relevant chunks: keyword search vs vector search (guide §9.2–§9.5).

  keyword (full-text index, Lucene/BM25): great for exact names and IDs,
                                          misses paraphrases.
  vector  (vector index, approx. nearest neighbours by cosine similarity):
                                          finds paraphrases, can return
                                          "similar but irrelevant" passages.

Both return the same row shape, so they can be compared and later combined
(M8 GraphRAG).
"""
import re

from app.retrieval.embedder import OllamaEmbedder

VECTOR_INDEX = "chunk_embedding"
FULLTEXT_INDEX = "chunk_text"


def ensure_indexes(driver, database: str, dimensions: int) -> None:
    # Dimensions + similarity are fixed at creation. Changing embedding model
    # => drop index, clear c.embedding, re-embed (plan a migration).
    driver.execute_query(
        f"""
        CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS
        FOR (c:Chunk) ON (c.embedding)
        OPTIONS {{ indexConfig: {{
            `vector.dimensions`: $dims,
            `vector.similarity_function`: 'cosine'
        }} }}
        """,
        dims=dimensions, database_=database,
    )
    driver.execute_query(
        f"CREATE FULLTEXT INDEX {FULLTEXT_INDEX} IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]", database_=database)
    driver.execute_query("CALL db.awaitIndexes(300)", database_=database)


def embed_missing_chunks(driver, database: str, embedder: OllamaEmbedder, batch_size: int = 16,
                         document_id: str | None = None) -> int:
    """Embed only chunks that have no vector yet, or one from a different model. Safe to re-run.
    `document_id` limits it to one document (what the worker does) — so a job can never touch
    other documents' vectors."""
    records, _, _ = driver.execute_query(
        """
        MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
        WHERE ($doc IS NULL OR d.id = $doc)
          AND (c.embedding IS NULL OR c.embeddingModel <> $model)
        RETURN c.id AS id, c.text AS text ORDER BY c.id
        """,
        model=embedder.model, doc=document_id, database_=database,
    )
    rows = [dict(r) for r in records]
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        vectors = embedder.embed_documents([r["text"] for r in batch])
        driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (c:Chunk {id: row.id})
            // stores the list as a compact float32 vector property
            CALL db.create.setNodeVectorProperty(c, 'embedding', row.vector)
            SET c.embeddingModel = $model, c.embeddedAt = datetime()
            """,
            rows=[{"id": r["id"], "vector": v} for r, v in zip(batch, vectors)],
            model=embedder.model, database_=database,
        )
    return len(rows)


# Neo4j 2026 replaced the db.index.*.queryNodes procedures with the SEARCH
# clause (Cypher 25). Exactly the "version-awareness" warning at the end of
# the guide: verify syntax against YOUR server version.
_RETURN = """
    MATCH (d:Document)-[:HAS_CHUNK]->(c)
    RETURN c.id AS chunk_id, d.filename AS document, c.page AS page,
           c.chunkIndex AS chunk_index, score, c.text AS text
    ORDER BY score DESC
"""


def vector_search(driver, database: str, embedder: OllamaEmbedder, question: str, k: int = 5) -> list[dict]:
    records, _, _ = driver.execute_query(
        f"""CYPHER 25
        MATCH (c:Chunk)
          SEARCH c IN (VECTOR INDEX {VECTOR_INDEX} FOR $vector LIMIT $k) SCORE AS score""" + _RETURN,
        k=k, vector=embedder.embed_query(question), database_=database,
    )
    return [dict(r) for r in records]


_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def lucene_escape(text: str) -> str:
    """User text is DATA: escape Lucene operators so 'C++ AND (x' can't break or alter the query.
    Special characters get a backslash; the operator WORDS AND/OR/NOT (case-sensitive in Lucene)
    are lower-cased so they become ordinary search terms."""
    text = _LUCENE_SPECIAL.sub(r"\\\1", text)
    return re.sub(r"\b(AND|OR|NOT)\b", lambda m: m.group(1).lower(), text)


def hybrid_search(driver, database: str, embedder: OllamaEmbedder, question: str, k: int = 5,
                  pool: int = 20, rrf_k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion: score = sum over lists of 1 / (rrf_k + rank).

    Keyword scores (BM25, unbounded) and cosine scores (0..1) aren't comparable,
    so we fuse RANKS, not scores. A chunk that both methods like rises to the top.
    """
    return rrf_fuse([keyword_search(driver, database, question, pool),
                     vector_search(driver, database, embedder, question, pool)], k, rrf_k)


def rrf_fuse(result_lists: list[list[dict]], k: int, rrf_k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    for results in result_lists:
        for rank, row in enumerate(results, start=1):
            entry = fused.setdefault(row["chunk_id"], {**row, "score": 0.0})
            entry["score"] += 1.0 / (rrf_k + rank)
    return sorted(fused.values(), key=lambda r: r["score"], reverse=True)[:k]


def keyword_search(driver, database: str, question: str, k: int = 5) -> list[dict]:
    records, _, _ = driver.execute_query(
        f"""CYPHER 25
        MATCH (c:Chunk)
          SEARCH c IN (FULLTEXT INDEX {FULLTEXT_INDEX} FOR $q LIMIT $k) SCORE AS score""" + _RETURN,
        q=lucene_escape(question), k=k, database_=database,
    )
    return [dict(r) for r in records]
