"""M5/M6 integration tests against the real Neo4j (skipped if it isn't running).

Everything is namespaced with a unique 'zz-test-…' tag and cleaned up after.
"""
import uuid

import pytest

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.extraction.validator import AcceptedEntity, AcceptedRelationship, Rejection, ValidationReport
from app.graph.entity_resolver import AmbiguousEntity, resolve_entity
from app.graph.graph_writer import delete_document, write_document_graph
from app.graph.repository import create_schema, upsert_entity, upsert_relationship


@pytest.fixture(scope="module")
def ctx():
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    db = get_settings().neo4j_database
    create_schema(driver, db)
    tag = f"zz-test-{uuid.uuid4().hex[:8]}"
    yield driver, db, tag
    driver.execute_query(
        """
        MATCH (d:Document) WHERE d.id STARTS WITH $t
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) OPTIONAL MATCH (c)<-[:FROM_CHUNK]-(ri)
        DETACH DELETE d, c, ri
        """, t=tag, database_=db)
    driver.execute_query("MATCH (e:Entity) WHERE e.name STARTS WITH $t DETACH DELETE e", t=tag, database_=db)
    close_driver()


def q(ctx, cypher, **params):
    driver, db, tag = ctx
    return driver.execute_query(cypher, t=tag, database_=db, **params).records


def doc_and_report(tag, n, *, harshit_spelling="Harshit", with_review=False):
    """Fake document `n` with one chunk saying <tag> Harshit works at <tag> Acme."""
    doc = {"id": f"{tag}-doc{n}", "filename": f"d{n}.txt", "sha256": "x", "size_bytes": 1, "status": "x"}
    chunk = {"id": f"{tag}-doc{n}-c0", "text": "...", "page": 1, "index": 0}
    h, acme = f"{tag} {harshit_spelling}", f"{tag} Acme"
    rep = ValidationReport(chunk_id=chunk["id"])
    rep.entities = [AcceptedEntity(h, "Person"), AcceptedEntity(acme, "Company")]
    rep.relationships = [AcceptedRelationship(h, "Person", "WORKS_AT", acme, "Company", {"role": "Eng"},
                                              f"quote from doc{n}", chunk["id"])]
    if with_review:
        rep.needs_review = [Rejection({"source": h, "type": "WORKS_ON", "target": "X"}, "evidence does not mention")]
    return doc, [chunk], [rep]


def write(ctx, doc, chunks, reports):
    driver, db, _ = ctx
    with driver.session(database=db) as s:
        return s.execute_write(write_document_graph, doc, chunks, reports, "fake-model", "v-test")


def works_at(ctx):
    return q(ctx, """MATCH (p:Entity)-[r:WORKS_AT]->(c:Entity) WHERE p.name STARTS WITH $t
                     RETURN p.name AS p, r.sourceDocIds AS docs, r.evidence AS ev, coalesce(r.manual,false) AS manual""")


# ------------------------------------------------------------------ resolution
def test_alias_resolves_to_existing_entity(ctx):
    driver, db, tag = ctx
    with driver.session(database=db) as s:
        first = s.execute_write(resolve_entity, "Company", f"{tag} TechNova")
        again = s.execute_write(resolve_entity, "Company", f"{tag}   Tech Nova")
        other_type = s.execute_write(resolve_entity, "Project", f"{tag} TechNova")
    assert first.created and not again.created and again.id == first.id and again.alias_added
    assert other_type.id != first.id                     # same name, different type -> separate
    aliases = q(ctx, "MATCH (e:Entity {id: $id}) RETURN e.aliases AS a", id=first.id)[0]["a"]
    assert aliases == [f"{tag}   Tech Nova".strip()]


def test_two_candidates_is_ambiguous_not_guessed(ctx):
    driver, db, tag = ctx
    with driver.session(database=db) as s:
        # bypass resolution on purpose to create two look-alike entities
        s.execute_write(upsert_entity, "Team", f"{tag} Core Team")
        s.execute_write(upsert_entity, "Team", f"{tag} CoreTeam")
        with pytest.raises(AmbiguousEntity):
            s.execute_write(resolve_entity, "Team", f"{tag} core-team")


# ------------------------------------------------------------------ writing
def test_write_is_idempotent(ctx):
    _, _, tag = ctx
    doc, chunks, reps = doc_and_report(tag, 1, with_review=True)
    write(ctx, doc, chunks, reps)
    snapshot = q(ctx, """MATCH (n) WHERE n.id STARTS WITH $t OR n.name STARTS WITH $t
                         OPTIONAL MATCH (n)-[r]-() RETURN count(DISTINCT n) AS n, count(DISTINCT r) AS r""")[0]
    write(ctx, doc, chunks, reps)
    write(ctx, doc, chunks, reps)
    again = q(ctx, """MATCH (n) WHERE n.id STARTS WITH $t OR n.name STARTS WITH $t
                      OPTIONAL MATCH (n)-[r]-() RETURN count(DISTINCT n) AS n, count(DISTINCT r) AS r""")[0]
    assert dict(snapshot) == dict(again)
    rows = works_at(ctx)
    assert len(rows) == 1 and rows[0]["docs"] == [doc["id"]] and rows[0]["ev"] == ["quote from doc1"]
    assert q(ctx, "MATCH (ri:ReviewItem)-[:FROM_CHUNK]->(c:Chunk) WHERE c.id STARTS WITH $t RETURN count(ri) AS n")[0]["n"] == 1


def test_two_documents_support_one_fact_and_delete_keeps_the_other(ctx):
    driver, db, tag = ctx
    d2, c2, r2 = doc_and_report(tag, 2, harshit_spelling="HARSHIT")     # alias spelling, same person
    write(ctx, d2, c2, r2)
    rows = works_at(ctx)
    assert len(rows) == 1 and sorted(rows[0]["docs"]) == [f"{tag}-doc1", f"{tag}-doc2"]

    with driver.session(database=db) as s:
        s.execute_write(delete_document, f"{tag}-doc1")
    rows = works_at(ctx)
    assert len(rows) == 1 and rows[0]["docs"] == [f"{tag}-doc2"]       # still supported by doc2

    with driver.session(database=db) as s:
        s.execute_write(delete_document, f"{tag}-doc2")
    assert works_at(ctx) == []                                            # no support left -> fact gone
    assert q(ctx, "MATCH (e:Entity) WHERE e.name STARTS WITH $t + ' Acme' RETURN count(e) AS n")[0]["n"] == 0


def test_manual_fact_survives_document_deletion(ctx):
    driver, db, tag = ctx
    with driver.session(database=db) as s:
        s.execute_write(upsert_relationship, "Person", f"{tag} Harshit", "WORKS_AT", "Company", f"{tag} Acme")
    d3, c3, r3 = doc_and_report(tag, 3)
    write(ctx, d3, c3, r3)
    with driver.session(database=db) as s:
        s.execute_write(delete_document, d3["id"])
    rows = works_at(ctx)
    assert len(rows) == 1 and rows[0]["manual"] and rows[0]["docs"] == []
