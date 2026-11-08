// =====================================================================
// LESSON 3 — Embeddings and search inside Neo4j (Milestone M7)
// Run after:  python -m scripts.m7_embed
// =====================================================================

// --- 1. What did we store? ---------------------------------------------
MATCH (c:Chunk)
RETURN c.id, c.page, c.embeddingModel, size(c.embedding) AS dims
LIMIT 5;

SHOW INDEXES YIELD name, type, labelsOrTypes, properties, options
WHERE type IN ["VECTOR", "FULLTEXT"];

// --- 2. Keyword search with the SEARCH clause (Neo4j 2026 / Cypher 25) --
CYPHER 25
MATCH (c:Chunk)
  SEARCH c IN (FULLTEXT INDEX chunk_text FOR "idempotency duplicate" LIMIT 5) SCORE AS score
RETURN c.page, score, left(c.text, 120);

// --- 3. "More like this": use one chunk's vector as the query -----------
// No embedding model needed here — we reuse a vector already in the graph.
MATCH (seed:Chunk) WHERE seed.text CONTAINS "CREATE versus MERGE"
WITH seed.embedding AS v, seed.id AS seedId LIMIT 1
CYPHER 25
MATCH (c:Chunk)
  SEARCH c IN (VECTOR INDEX chunk_embedding FOR v LIMIT 6) SCORE AS score
WHERE c.id <> seedId
RETURN c.page, round(score, 3) AS score, left(c.text, 120);
// (If your Browser rejects CYPHER 25 mid-query, run the SEARCH part alone
//  after   :param v => [...]   or use scripts/m7_search.py.)

// --- 4. Cosine similarity by hand between two chunks ---------------------
MATCH (a:Chunk {chunkIndex: 0})<-[:HAS_CHUNK]-(:Document {filename: "sample_company.txt"})
MATCH (b:Chunk {chunkIndex: 1})<-[:HAS_CHUNK]-(:Document {filename: "sample_company.txt"})
RETURN vector.similarity.cosine(a.embedding, b.embedding) AS similarity;
// Compare with two chunks from DIFFERENT documents. Which is higher? Why?

// --- 5. The most similar pairs of chunks in the whole graph (brute force)-
// Fine for ~60 chunks. On 100k chunks this is why a vector INDEX exists.
MATCH (a:Chunk), (b:Chunk) WHERE a.id < b.id
RETURN a.page, b.page, round(vector.similarity.cosine(a.embedding, b.embedding), 3) AS sim
ORDER BY sim DESC LIMIT 10;
