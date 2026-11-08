// =====================================================================
// LESSON 4 — The graph half of GraphRAG, by hand (Milestone M8)
// These are the queries app/retrieval/graph_retriever.py runs for you.
// =====================================================================

// --- 1. Entity linking: which entities does a question mention? -------
:param question => "which project does harshit work on at tech nova?"
MATCH (e:Entity)
WHERE any(form IN [e.name] + coalesce(e.aliases, [])
          WHERE $question CONTAINS toLower(form))
RETURN e.name, e.type, e.aliases;
// (Python uses whole-word regex matching; CONTAINS would also match "Ravi" inside "Ravindra".)

// --- 2. Bounded traversal along ONTOLOGY relationships only ------------
// Why not -[*1..2]- ?  Try it: MENTIONS goes through Chunks, so every entity
// in the same paragraph becomes a "neighbour". Compare the two results.
MATCH path = (e:Entity {name: "Harshit"})-[:WORKS_AT|WORKS_ON|USES|OWNS|MEMBER_OF|FOUNDED|COLLABORATES_WITH*1..2]-(:Entity)
RETURN path;

MATCH path = (e:Entity {name: "Harshit"})-[*1..2]-()
RETURN path LIMIT 50;      // the noisy version

// --- 3. Facts WITH their evidence (only document-backed ones) ----------
MATCH path = (e:Entity {name: "Harshit"})-[:WORKS_AT|WORKS_ON|USES|OWNS|MEMBER_OF|FOUNDED|COLLABORATES_WITH*1..2]-(:Entity)
UNWIND relationships(path) AS r
WITH DISTINCT r WHERE size(coalesce(r.sourceDocIds, [])) > 0
RETURN startNode(r).name AS source, type(r) AS rel, endNode(r).name AS target, r.evidence[0] AS quote;

// --- 4. Two-hop question the vector index can't answer alone ------------
// "Who works on the same project as Harshit?"
MATCH (:Person {name: "Harshit"})-[:WORKS_ON]->(p:Project)<-[:WORKS_ON]-(colleague:Person)
RETURN p.name, collect(colleague.name) AS colleagues;

// --- 5. Facts the graph knows but cannot CITE (manual seed facts) --------
// GraphRAG leaves these out of answers: no document, no citation.
MATCH (a)-[r]->(b) WHERE r.manual AND size(coalesce(r.sourceDocIds, [])) = 0
RETURN a.name, type(r), b.name;
