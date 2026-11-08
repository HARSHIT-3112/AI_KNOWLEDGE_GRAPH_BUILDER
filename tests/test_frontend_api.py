"""Frontend support tests: /graph shape, auth, documents toggle, and the static UI being served."""
import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.core.config import get_settings
from app.db.neo4j import get_driver
from app.graph.ontology import ENTITY_TYPES, RELATION_TYPES


@pytest.fixture(scope="module")
def client():
    try:
        get_driver().verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    with TestClient(app) as c:
        c.headers["X-API-Key"] = get_settings().api_key
        yield c


def test_graph_requires_api_key(client):
    assert client.get("/graph", headers={"X-API-Key": "wrong"}).status_code == 401


def test_graph_has_consistent_nodes_and_links(client):
    g = client.get("/graph").json()
    ids = {n["id"] for n in g["nodes"]}
    assert all(n["type"] in ENTITY_TYPES for n in g["nodes"])
    for link in g["links"]:
        assert link["source"] in ids and link["target"] in ids        # no dangling links -> UI won't crash
        assert link["type"] in RELATION_TYPES                          # never MENTIONS/HAS_CHUNK by default
        assert link["sourced"] == (link["evidence_count"] > 0)
    assert g["stats"]["links"] == len(g["links"])


def test_documents_toggle_adds_document_nodes_and_mentions(client):
    with_docs = client.get("/graph", params={"include_documents": True}).json()
    docs = [n for n in with_docs["nodes"] if n["type"] == "Document"]
    ids = {n["id"] for n in with_docs["nodes"]}
    mentions = [l for l in with_docs["links"] if l["type"] == "MENTIONS"]
    assert all(l["source"] in ids and l["target"] in ids for l in mentions)
    if docs:
        assert mentions and all(l["source"].startswith("doc-") for l in mentions)


def test_ui_is_served_and_root_redirects(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and "Knowledge Graph Explorer" in page.text
    js = client.get("/ui/app.js").text
    assert "textContent" in js and "escapeHtml" in js                 # untrusted text is never raw HTML


def test_ui_needs_no_key_but_data_does(client):
    assert client.get("/ui/", headers={"X-API-Key": ""}).status_code == 200
    assert client.get("/graph", headers={"X-API-Key": ""}).status_code == 401
