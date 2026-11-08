"""Integration tests against the real Neo4j (skipped if it isn't running).

They only touch nodes named with a unique 'zz-test-…' prefix and delete just
those afterwards, so your learning graph is never wiped.
"""
import uuid

import pytest
from neo4j.exceptions import ConstraintError

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.graph.ids import entity_id
from app.graph.repository import create_schema, upsert_document_with_chunks, upsert_relationship


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
    driver.execute_query("MATCH (e:Entity) WHERE e.name STARTS WITH $t DETACH DELETE e", t=tag, database_=db)
    driver.execute_query("MATCH (d:Document) WHERE d.id STARTS WITH $t "
                         "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) DETACH DELETE d, c", t=tag, database_=db)
    close_driver()


def _count(driver, db, tag):
    recs, _, _ = driver.execute_query(
        "MATCH (e:Entity) WHERE e.name STARTS WITH $t "
        "OPTIONAL MATCH (e)-[r]->() RETURN count(DISTINCT e) AS n, count(r) AS r",
        t=tag, database_=db)
    return recs[0]["n"], recs[0]["r"]


def test_upsert_is_idempotent(ctx):
    driver, db, tag = ctx
    write = lambda tx: upsert_relationship(tx, "Person", f"{tag} Alice", "WORKS_AT", "Company", f"{tag} Acme", {"role": "Eng"})
    with driver.session(database=db) as s:
        s.execute_write(write)
        first = _count(driver, db, tag)
        s.execute_write(write)
        s.execute_write(write)
    assert first == (2, 1)
    assert _count(driver, db, tag) == first


def test_uniqueness_constraint_blocks_duplicates(ctx):
    driver, db, tag = ctx
    eid = entity_id("Person", f"{tag} Bob")
    driver.execute_query("CREATE (:Entity {id: $id, name: $n})", id=eid, n=f"{tag} Bob", database_=db)
    with pytest.raises(ConstraintError):
        driver.execute_query("CREATE (:Entity {id: $id, name: $n})", id=eid, n=f"{tag} Bob", database_=db)


def test_failed_transaction_rolls_back(ctx):
    driver, db, tag = ctx

    def half_bad(tx):
        upsert_relationship(tx, "Person", f"{tag} Carol", "WORKS_ON", "Project", f"{tag} X")
        upsert_relationship(tx, "Person", f"{tag} Carol", "OWNS", "Project", f"{tag} X")  # not allowed

    before = _count(driver, db, tag)
    with driver.session(database=db) as s, pytest.raises(ValueError):
        s.execute_write(half_bad)
    assert _count(driver, db, tag) == before        # the valid first write was rolled back too


def test_reprocessing_replaces_stale_chunks(ctx):
    driver, db, tag = ctx
    doc = {"id": f"{tag}-doc", "filename": "t.txt", "sha256": "x", "size_bytes": 1, "status": "chunked"}
    mk = lambda ids: [{"id": f"{tag}-{i}", "text": i, "page": 1, "index": n} for n, i in enumerate(ids)]
    with driver.session(database=db) as s:
        s.execute_write(upsert_document_with_chunks, doc, mk(["a", "b", "c"]))
        s.execute_write(upsert_document_with_chunks, doc, mk(["a", "d"]))
    recs, _, _ = driver.execute_query(
        "MATCH (:Document {id: $d})-[:HAS_CHUNK]->(c) RETURN collect(c.id) AS ids", d=doc["id"], database_=db)
    assert sorted(recs[0]["ids"]) == [f"{tag}-a", f"{tag}-d"]
