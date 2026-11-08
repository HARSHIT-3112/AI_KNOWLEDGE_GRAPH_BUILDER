"""M5 + M6 — Write validated facts into Neo4j, with provenance (guide §8.3–§8.8).

What ends up in the graph for one document:

  (:Document)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(:Entity)
  (:Entity)-[:WORKS_AT {role,
                        sourceDocIds:   ["doc-1", "doc-1"],      <- parallel lists:
                        sourceChunkIds: ["chunk-a", "chunk-b"],  <- entry i = one piece
                        evidence:       ["quote a", "quote b"],  <- of supporting evidence
                        model, promptVersion}]->(:Entity)
  (:ReviewItem {status:'pending', reason, payload})-[:FROM_CHUNK]->(:Chunk)

Why parallel lists instead of a separate (:Fact) node? Fewer moving parts while
learning, and native relationship types stay traversable. The guide's (:Fact)
model is the advanced alternative once facts need their own lifecycle.

REPROCESSING POLICY ("replace"): before writing a document's new extraction,
its old support is removed from every relationship; a relationship whose
support becomes empty is deleted — unless it was created by hand
(r.manual = true, e.g. the seed script). Then the new
facts are written. Everything runs in ONE transaction, so a crash half-way
leaves the previous state intact, and running it twice gives the same graph.
"""
import hashlib
import json
from dataclasses import asdict, dataclass, field

from neo4j import ManagedTransaction

from app.extraction.validator import ValidationReport
from app.graph.entity_resolver import AmbiguousEntity, resolve_entity
from app.graph.ids import relation_id
from app.graph.ontology import RELATION_TYPES
from app.graph.repository import upsert_document_with_chunks


@dataclass
class WriteStats:
    entities_created: int = 0
    entities_reused: int = 0
    aliases_added: int = 0
    mentions: int = 0
    relationships_written: int = 0
    review_items: int = 0
    resolution_conflicts: list[str] = field(default_factory=list)


def remove_document_support(tx: ManagedTransaction, doc_id: str) -> dict:
    """Undo everything a document contributed. Used by reprocessing AND deletion."""
    # 1. strip this document's entries out of the parallel provenance lists
    tx.run(
        """
        MATCH ()-[r]->() WHERE $doc IN r.sourceDocIds
        WITH r, [i IN range(0, size(r.sourceDocIds) - 1) WHERE r.sourceDocIds[i] <> $doc] AS keep
        WITH r, [i IN keep | r.sourceDocIds[i]]   AS docs,
                [i IN keep | r.sourceChunkIds[i]] AS chunks,
                [i IN keep | r.evidence[i]]       AS quotes
        SET r.sourceDocIds = docs, r.sourceChunkIds = chunks, r.evidence = quotes
        """,
        doc=doc_id,
    )
    # 2. facts supported ONLY by this document are gone now
    #    (hand-made facts, r.manual = true, survive even with no document support)
    deleted_rels = tx.run(
        "MATCH ()-[r]->() WHERE r.sourceDocIds = [] AND NOT coalesce(r.manual, false) DELETE r RETURN count(r) AS n"
    ).single()["n"]
    # 3. entities this document mentioned that are now connected to nothing
    deleted_entities = tx.run(
        """
        MATCH (:Document {id: $doc})-[:HAS_CHUNK]->(:Chunk)-[m:MENTIONS]->(e:Entity)
        WITH collect(DISTINCT e) AS mentioned, collect(m) AS ms
        FOREACH (m IN ms | DELETE m)
        WITH mentioned
        UNWIND mentioned AS e
        WITH e WHERE NOT (e)--()
        DELETE e
        RETURN count(e) AS n
        """,
        doc=doc_id,
    ).single()
    # 4. pending review items from this document
    tx.run("MATCH (:Document {id: $doc})-[:HAS_CHUNK]->(:Chunk)<-[:FROM_CHUNK]-(ri:ReviewItem) DETACH DELETE ri",
           doc=doc_id)
    return {"relationships_deleted": deleted_rels, "entities_deleted": deleted_entities["n"] if deleted_entities else 0}


def delete_document(tx: ManagedTransaction, doc_id: str) -> dict:
    """Interview question from the guide: delete a document and all facts supported ONLY by it."""
    stats = remove_document_support(tx, doc_id)
    tx.run("MATCH (d:Document {id: $doc}) OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) DETACH DELETE d, c", doc=doc_id)
    return stats


def _review(tx: ManagedTransaction, chunk_id: str, payload: dict, reason: str) -> None:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    rid = "review-" + hashlib.sha256(f"{chunk_id}\x1f{body}\x1f{reason}".encode()).hexdigest()[:16]
    tx.run(
        """
        MATCH (c:Chunk {id: $chunk})
        MERGE (ri:ReviewItem {id: $id})
        ON CREATE SET ri.createdAt = datetime(), ri.status = 'pending'
        SET ri.payload = $payload, ri.reason = $reason
        MERGE (ri)-[:FROM_CHUNK]->(c)
        """,
        chunk=chunk_id, id=rid, payload=body, reason=reason,
    )


def write_document_graph(
    tx: ManagedTransaction, doc: dict, chunks: list[dict], reports: list[ValidationReport],
    model: str, prompt_version: str,
) -> WriteStats:
    stats = WriteStats()
    remove_document_support(tx, doc["id"])
    upsert_document_with_chunks(tx, {**doc, "status": doc.get("status", "graph_written")}, chunks)
    tx.run("MATCH (d:Document {id: $id}) SET d.model = $model, d.promptVersion = $pv",
           id=doc["id"], model=model, pv=prompt_version)

    for report in reports:
        ids: dict[tuple[str, str], str] = {}                 # (type, name as extracted) -> entity id
        for e in report.entities:
            try:
                res = resolve_entity(tx, e.type, e.name)
            except AmbiguousEntity as exc:
                stats.resolution_conflicts.append(str(exc))
                _review(tx, report.chunk_id, asdict(e), f"entity resolution: {exc}")
                continue
            ids[(e.type, e.name)] = res.id
            stats.entities_created += res.created
            stats.entities_reused += not res.created
            stats.aliases_added += res.alias_added
            tx.run("MATCH (c:Chunk {id: $c}), (e:Entity {id: $e}) MERGE (c)-[:MENTIONS]->(e)",
                   c=report.chunk_id, e=res.id)
            stats.mentions += 1

        for r in report.relationships:
            src, tgt = ids.get((r.source_type, r.source)), ids.get((r.target_type, r.target))
            if src is None or tgt is None:
                continue                                     # endpoint went to review above
            assert r.type in RELATION_TYPES                  # validated already; belt and braces
            tx.run(
                f"""
                MATCH (s:Entity {{id: $src}}), (t:Entity {{id: $tgt}})
                MERGE (s)-[rel:{r.type} {{id: $rid}}]->(t)
                SET rel += $props,
                    rel.sourceDocIds   = coalesce(rel.sourceDocIds, [])   + $doc,
                    rel.sourceChunkIds = coalesce(rel.sourceChunkIds, []) + $chunk,
                    rel.evidence       = coalesce(rel.evidence, [])       + $quote,
                    rel.model = $model, rel.promptVersion = $pv, rel.extractedAt = datetime()
                """,
                src=src, tgt=tgt, rid=relation_id(src, r.type, tgt), props=r.properties,
                doc=doc["id"], chunk=r.chunk_id, quote=r.evidence, model=model, pv=prompt_version,
            )
            stats.relationships_written += 1

        for item in report.needs_review:
            _review(tx, report.chunk_id, item.item, item.reason)
            stats.review_items += 1
    return stats
