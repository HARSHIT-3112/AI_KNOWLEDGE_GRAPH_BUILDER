// =====================================================================
// LESSON 1 — Cypher foundations (guide §3–§4, Milestone M1)
// Open http://localhost:7475, log in (user neo4j, password from .env,
// connect URL bolt://localhost:7688) and run these ONE BLOCK AT A TIME.
// Before running each block, predict what it will return. Then check.
// =====================================================================

// --- 1. CREATE nodes ---------------------------------------------------
// A node = an entity. :Person is its LABEL. {..} are PROPERTIES.
CREATE (p:Person {id: "person-001", name: "Harshit"})
RETURN p;

CREATE (c:Company {id: "company-001", name: "TechNova"})
RETURN c;

// --- 2. MATCH + RETURN -----------------------------------------------
MATCH (p:Person {name: "Harshit"}) RETURN p;

// --- 3. CREATE a relationship (directed, typed, can hold properties) ---
MATCH (p:Person {id: "person-001"})
MATCH (c:Company {id: "company-001"})
CREATE (p)-[r:WORKS_AT {role: "ML Engineer"}]->(c)
RETURN p, r, c;      // return r too, or the Browser won't draw the edge

// --- 4. CREATE vs MERGE  (the key idea behind idempotency) -----------
// Run this block TWICE, then count. CREATE makes duplicates:
CREATE (:Person {id: "dup-test", name: "Dup"});
MATCH (p:Person {id: "dup-test"}) RETURN count(p);   // -> 2 after two runs

// MERGE = "match it, or create it if missing". Run twice -> still 1:
MERGE (p:Person {id: "merge-test"})
ON CREATE SET p.name = "Merge", p.createdAt = datetime()
ON MATCH  SET p.lastSeenAt = datetime()
RETURN p;

// clean up the experiment (targeted delete, never MATCH (n) DETACH DELETE n)
MATCH (p:Person) WHERE p.id IN ["dup-test", "merge-test"] DETACH DELETE p;

// --- 5. UPDATE ------------------------------------------------------
MATCH (p:Person {id: "person-001"}) SET p.title = "AI Engineer" RETURN p;

// --- 6. Constraints: make the DATABASE enforce identity -------------
CREATE CONSTRAINT person_id_unique IF NOT EXISTS
FOR (p:Person) REQUIRE p.id IS UNIQUE;
SHOW CONSTRAINTS;

// Now try to break it — this should FAIL with a constraint error:
CREATE (:Person {id: "person-001", name: "Impostor"});

// --- 7. Parameters (what Python will do) ----------------------------
// In Browser:   :param person_id => "person-001"
MATCH (p:Person {id: $person_id}) RETURN p;

// --- 8. Clean up the hand-made lesson nodes (so names don't clash below) --
MATCH (n) WHERE n.id IN ["person-001", "company-001"] DETACH DELETE n;
DROP CONSTRAINT person_id_unique IF EXISTS;

// =====================================================================
// After running `python scripts/m1_seed_sample_graph.py`, try these:
// =====================================================================

// Who works where? (aggregation)
MATCH (p:Person)-[:WORKS_AT]->(c:Company)
RETURN c.name AS company, collect(p.name) AS people, count(p) AS headcount
ORDER BY headcount DESC;

// Colleagues of Harshit (2-hop traversal)
MATCH (me:Person {name: "Harshit"})-[:WORKS_AT]->(c)<-[:WORKS_AT]-(colleague)
RETURN DISTINCT colleague.name;

// Technologies used by projects owned by TechNova (3-hop)
MATCH (:Company {name: "TechNova"})-[:OWNS]->(proj:Project)-[:USES]->(t:Technology)
RETURN proj.name, collect(t.name) AS stack;

// Shortest path between two people (bounded!)
MATCH path = shortestPath(
  (a:Person {name: "Harshit"})-[*..6]-(b:Person {name: "Ravi"})
)
RETURN path;

// Everything within 2 hops of Harshit (visualise it)
MATCH path = (:Person {name: "Harshit"})-[*1..2]-() RETURN path LIMIT 50;
