"""Frontend support: the whole knowledge graph as {nodes, links} for the 3D view.

Only knowledge-graph nodes are sent (entities, optionally documents). Chunks
are left out: 60+ text nodes would bury the entities, and their text is
fetched on demand when you click something.

Each link says whether it is SOURCED (has document evidence) or MANUAL (hand
entered, no citation) so the UI can draw the difference.
"""
from app.graph.ontology import RELATION_TYPES

_REL = "|".join(sorted(RELATION_TYPES))      # allow-listed relationship types only


def build_graph(driver, database: str, include_documents: bool = False) -> dict:
    nodes = [
        {"id": r["id"], "name": r["name"], "type": r["type"], "aliases": r["aliases"], "degree": r["degree"]}
        for r in driver.execute_query(
            f"""
            MATCH (e:Entity)
            RETURN e.id AS id, e.name AS name, e.type AS type, coalesce(e.aliases, []) AS aliases,
                   COUNT {{ (e)-[:{_REL}]-() }} AS degree
            ORDER BY name
            """, database_=database).records
    ]
    links = [
        {"id": r["id"], "source": r["source"], "target": r["target"], "type": r["type"],
         "properties": {k: v for k, v in r["props"].items() if v is not None},
         "evidence_count": r["evidence_count"], "sourced": r["evidence_count"] > 0,
         "documents": r["documents"]}
        for r in driver.execute_query(
            f"""
            MATCH (a:Entity)-[r:{_REL}]->(b:Entity)
            RETURN coalesce(r.id, elementId(r)) AS id, a.id AS source, b.id AS target, type(r) AS type,
                   {{role: r.role, since: r.since}} AS props,
                   size(coalesce(r.sourceChunkIds, [])) AS evidence_count,
                   [x IN coalesce(r.sourceDocIds, []) | x] AS documents
            """, database_=database).records
    ]
    for link in links:                                    # unique document ids, order kept
        link["documents"] = list(dict.fromkeys(link["documents"]))

    if include_documents:
        for r in driver.execute_query(
            """
            MATCH (d:Document)
            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e:Entity)
            RETURN d.id AS id, d.filename AS name, d.status AS status, collect(DISTINCT e.id) AS mentions
            """, database_=database).records:
            nodes.append({"id": r["id"], "name": r["name"], "type": "Document", "status": r["status"],
                          "aliases": [], "degree": len(r["mentions"])})
            links.extend({"id": f"{r['id']}->{eid}", "source": r["id"], "target": eid, "type": "MENTIONS",
                          "properties": {}, "evidence_count": 0, "sourced": True, "documents": [r["id"]]}
                         for eid in r["mentions"])

    by_type: dict[str, int] = {}
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    return {"nodes": nodes, "links": links,
            "stats": {"nodes_by_type": by_type, "links": len(links),
                      "sourced_links": sum(1 for x in links if x["sourced"] and x["type"] != "MENTIONS")}}
