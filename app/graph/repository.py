"""M2 — Graph repository: every Cypher query the app runs lives here.

Rules followed (guide §4.9, §8.5, §12.4):
  * VALUES are always passed as $parameters (prevents Cypher injection).
  * LABELS / RELATIONSHIP TYPES cannot be parameters in Cypher, so they are
    checked against the ontology allow-list BEFORE being put in the query.
  * Writes use MERGE on deterministic ids -> running twice changes nothing.
  * Write functions are "transaction functions" (first arg tx) so the driver
    can retry them safely on transient errors.
"""
from neo4j import ManagedTransaction

from app.graph.ids import compact_key, entity_id, normalize_name, relation_id
from app.graph.ontology import ALLOWED_REL_PROPERTIES, check_entity_type, check_relation

SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    "CREATE INDEX entity_canonical_name IF NOT EXISTS FOR (e:Entity) ON (e.canonicalName)",
    "CREATE INDEX entity_type_compact IF NOT EXISTS FOR (e:Entity) ON (e.type, e.compactName)",
    "CREATE CONSTRAINT review_id IF NOT EXISTS FOR (r:ReviewItem) REQUIRE r.id IS UNIQUE",
]


def create_schema(driver, database: str) -> None:
    for stmt in SCHEMA_STATEMENTS:
        driver.execute_query(stmt, database_=database)
    # Backfill: entities created before M5 have no compactName yet.
    records, _, _ = driver.execute_query(
        "MATCH (e:Entity) WHERE e.compactName IS NULL RETURN e.id AS id, e.name AS name", database_=database)
    if records:
        driver.execute_query(
            "UNWIND $rows AS row MATCH (e:Entity {id: row.id}) SET e.compactName = row.ck",
            rows=[{"id": r["id"], "ck": compact_key(r["name"])} for r in records], database_=database)


# ---------------------------------------------------------------- entities
def upsert_entity(tx: ManagedTransaction, entity_type: str, name: str) -> str:
    label = check_entity_type(entity_type)          # allow-listed -> safe to format
    eid = entity_id(label, name)
    tx.run(
        f"""
        MERGE (e:Entity {{id: $id}})
        ON CREATE SET e.createdAt = datetime()
        SET e:{label}, e.name = $name, e.canonicalName = $canonical,
            e.compactName = $compact, e.type = $type
        """,
        id=eid, name=name.strip(), canonical=normalize_name(name), compact=compact_key(name), type=label,
    )
    return eid


# ----------------------------------------------------------- relationships
def upsert_relationship(
    tx: ManagedTransaction,
    source_type: str, source_name: str,
    rel_type: str,
    target_type: str, target_name: str,
    properties: dict | None = None,
) -> str:
    from app.graph.entity_resolver import resolve_entity   # M5: go through resolution (lazy: avoids import cycle)

    rtype = check_relation(rel_type, source_type, target_type)   # allow-listed
    src_id = resolve_entity(tx, source_type, source_name).id
    tgt_id = resolve_entity(tx, target_type, target_name).id
    rid = relation_id(src_id, rtype, tgt_id)
    props = {k: v for k, v in (properties or {}).items() if k in ALLOWED_REL_PROPERTIES and v is not None}
    tx.run(
        f"""
        MATCH (s:Entity {{id: $src}}), (t:Entity {{id: $tgt}})
        MERGE (s)-[r:{rtype} {{id: $rid}}]->(t)
        SET r += $props, r.manual = true      // hand-entered fact: reprocessing never deletes it
        """,
        src=src_id, tgt=tgt_id, rid=rid, props=props,
    )
    return rid


# ------------------------------------------------- documents & provenance
def upsert_document_with_chunks(tx: ManagedTransaction, doc: dict, chunks: list[dict]) -> None:
    """(:Document)-[:HAS_CHUNK]->(:Chunk), plus (:Chunk)-[:NEXT]->(:Chunk) for reading order.

    UNWIND turns the list parameter into rows -> one round-trip for all chunks.
    Old chunks of this document that no longer exist (e.g. after changing the
    chunk size) are removed so reprocessing never leaves stale chunks behind.
    """
    tx.run(
        """
        MERGE (d:Document {id: $doc.id})
        ON CREATE SET d.createdAt = datetime()
        SET d.filename = $doc.filename, d.sha256 = $doc.sha256,
            d.sizeBytes = $doc.size_bytes, d.status = $doc.status, d.updatedAt = datetime()
        WITH d
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(old:Chunk)
        WHERE NOT old.id IN [c IN $chunks | c.id]
        DETACH DELETE old
        """,
        doc=doc, chunks=chunks,
    )
    tx.run(
        """
        MATCH (d:Document {id: $doc_id})
        UNWIND $chunks AS c
        MERGE (ch:Chunk {id: c.id})
        SET ch.text = c.text, ch.page = c.page, ch.chunkIndex = c.index
        MERGE (d)-[:HAS_CHUNK]->(ch)
        """,
        doc_id=doc["id"], chunks=chunks,
    )
    tx.run(
        """
        MATCH (d:Document {id: $doc_id})-[:HAS_CHUNK]->(a:Chunk)
        MATCH (d)-[:HAS_CHUNK]->(b:Chunk {chunkIndex: a.chunkIndex + 1})
        MERGE (a)-[:NEXT]->(b)
        """,
        doc_id=doc["id"],
    )


# ------------------------------------------------------------------ reads
def count_graph(driver, database: str) -> dict:
    records, _, _ = driver.execute_query(
        """
        CALL () { MATCH (n) RETURN count(n) AS nodes }
        CALL () { MATCH ()-[r]->() RETURN count(r) AS rels }
        RETURN nodes, rels
        """,
        database_=database,
    )
    return dict(records[0])


def neighbours(driver, database: str, name: str) -> list[dict]:
    records, _, _ = driver.execute_query(
        """
        MATCH (e:Entity {canonicalName: $canonical})-[r]-(other:Entity)
        RETURN e.name AS entity, type(r) AS rel,
               startNode(r) = e AS outgoing, other.name AS other, other.type AS otherType,
               properties(r) AS props
        ORDER BY rel, other
        """,
        canonical=normalize_name(name), database_=database,
    )
    return [dict(r) for r in records]
