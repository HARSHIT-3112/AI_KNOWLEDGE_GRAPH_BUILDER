"""M8 — The GRAPH half of GraphRAG (guide §9.3).

  1. Entity linking: which known entities does the question mention?
     Deterministic: whole-word match of each entity's name or alias in the
     question ("Who works with technova's ML engineer?" -> TechNova).
  2. Traversal: walk 1–2 hops from those entities, ONLY along ontology
     relationship types (not MENTIONS/HAS_CHUNK, which would hop through
     chunks and link everything to everything).
  3. Keep only facts that have document evidence, and bring that evidence
     (quote + document + page) with them so the answer can cite it.

Hand-made seed facts (manual, no document) are left out on purpose: an answer
must be traceable to a source, and those have none.
"""
import re
from dataclasses import dataclass, field

from app.graph.ids import normalize_name
from app.graph.ontology import RELATION_TYPES

_REL_PATTERN = "|".join(sorted(RELATION_TYPES))      # allow-listed -> safe to put in the query


@dataclass(frozen=True)
class LinkedEntity:
    id: str
    name: str
    type: str
    matched: str          # the surface form found in the question


@dataclass
class GraphFact:
    source: str
    rel: str
    target: str
    properties: dict
    hops: int
    evidence: list[dict] = field(default_factory=list)   # [{quote, chunk_id, document, page}]

    def sentence(self) -> str:
        props = ", ".join(f"{k}: {v}" for k, v in self.properties.items())
        return f"({self.source})-[:{self.rel}{' {' + props + '}' if props else ''}]->({self.target})"


def link_entities(driver, database: str, question: str) -> list[LinkedEntity]:
    records, _, _ = driver.execute_query(
        "MATCH (e:Entity) RETURN e.id AS id, e.name AS name, e.type AS type, coalesce(e.aliases, []) AS aliases",
        database_=database,
    )
    q = normalize_name(question)
    found: dict[str, LinkedEntity] = {}
    for r in records:
        for form in sorted({r["name"], *r["aliases"]}, key=len, reverse=True):
            if re.search(rf"(?<!\w){re.escape(normalize_name(form))}(?!\w)", q):
                found.setdefault(r["id"], LinkedEntity(r["id"], r["name"], r["type"], form))
                break
    return list(found.values())


def neighbourhood_facts(driver, database: str, entity_ids: list[str], max_hops: int = 2,
                        limit: int = 15) -> list[GraphFact]:
    if not entity_ids:
        return []
    max_hops = max(1, min(int(max_hops), 3))          # bounded traversal (guide §4.7)
    records, _, _ = driver.execute_query(
        f"""
        MATCH path = (e:Entity)-[:{_REL_PATTERN}*1..{max_hops}]-(:Entity)
        WHERE e.id IN $ids
        UNWIND range(0, length(path) - 1) AS i
        WITH relationships(path)[i] AS r, i + 1 AS hops
        WHERE size(coalesce(r.sourceDocIds, [])) > 0
        WITH r, min(hops) AS hops
        ORDER BY hops, size(r.sourceChunkIds) DESC
        LIMIT $limit
        UNWIND range(0, size(r.sourceChunkIds) - 1) AS j
        MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {{id: r.sourceChunkIds[j]}})
        WITH r, hops, collect({{quote: r.evidence[j], chunk_id: c.id, document: d.filename, page: c.page}}) AS ev
        RETURN startNode(r).name AS source, type(r) AS rel, endNode(r).name AS target,
               {{role: r.role, since: r.since}} AS props, hops, ev
        ORDER BY hops, source, rel
        """,
        ids=entity_ids, limit=limit, database_=database,
    )
    return [
        GraphFact(r["source"], r["rel"], r["target"], {k: v for k, v in r["props"].items() if v is not None},
                  r["hops"], r["ev"])
        for r in records
    ]
