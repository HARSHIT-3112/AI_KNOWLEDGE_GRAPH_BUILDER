// =====================================================================
// LESSON 2 — Provenance, aliases and review (Milestones M5 + M6)
// Run after:  python -m scripts.m5_write_graph documents/sample_company.txt
// Neo4j Browser: http://localhost:7475  (bolt://localhost:7688)
// =====================================================================

// --- 1. The whole document graph: Document -> Chunks -> Entities --------
MATCH p = (:Document)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(:Entity)
RETURN p;

// --- 2. "Why is this fact in the graph?"  (the core M6 question) -------
// Every extracted relationship carries parallel lists: entry i of
// sourceChunkIds / evidence / sourceDocIds is one piece of support.
MATCH (a:Person {name: "Harshit"})-[r:WORKS_ON]->(b:Project {name: "VisionAI"})
UNWIND range(0, size(r.sourceChunkIds) - 1) AS i
MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {id: r.sourceChunkIds[i]})
RETURN d.filename, c.page, c.chunkIndex, r.evidence[i] AS quote, r.model, r.promptVersion;

// --- 3. Facts backed by MORE than one chunk (stronger support) ---------
MATCH (a)-[r]->(b) WHERE size(r.sourceChunkIds) > 1
RETURN a.name, type(r), b.name, r.evidence;

// --- 4. Hand-made facts vs document-backed facts -----------------------
MATCH ()-[r]->() WHERE r.manual OR r.sourceDocIds IS NOT NULL
RETURN CASE WHEN size(coalesce(r.sourceDocIds, [])) > 0 THEN "has document evidence"
            ELSE "hand-made only" END AS kind, count(*) AS facts;

// --- 5. RECALL check: seed facts (our answer key) the LLM did NOT find --
MATCH (a)-[r]->(b) WHERE r.manual AND size(coalesce(r.sourceDocIds, [])) = 0
RETURN a.name, type(r), b.name ORDER BY a.name;
// Ask yourself for each one: is it really in the text? Which chunk?
// Was it split across chunks, said with a pronoun, or just missed?

// --- 6. Entity resolution at work ---------------------------------------
MATCH (e:Entity) WHERE size(e.aliases) > 0
RETURN e.name, e.aliases, e.compactName;

// --- 7. Human review queue -----------------------------------------------
MATCH (ri:ReviewItem {status: "pending"})-[:FROM_CHUNK]->(c:Chunk)
RETURN ri.reason, ri.payload, c.text;

// Approve or reject one (in a real app this is a UI button that also writes the fact):
// MATCH (ri:ReviewItem {id: "review-..."}) SET ri.status = "rejected", ri.reviewedAt = datetime();

// --- 8. Everything a document contributed --------------------------------
MATCH (d:Document {filename: "sample_company.txt"})
MATCH (a)-[r]->(b) WHERE d.id IN r.sourceDocIds
RETURN a.name, type(r), b.name;

// --- 9. Multi-hop with evidence: who works on projects that use Neo4j? ---
MATCH (p:Person)-[w:WORKS_ON]->(proj:Project)-[u:USES]->(:Technology {name: "Neo4j"})
RETURN p.name, proj.name, w.evidence AS whyWorksOn, u.evidence AS whyUses;
