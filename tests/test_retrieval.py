"""M7 tests: embedder contract (fake Ollama), query escaping, and real vector/keyword/hybrid search
against Neo4j using hand-made vectors (no embedding model needed)."""
import json
import uuid

import httpx
import pytest

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.retrieval.embedder import OllamaEmbedder
from app.retrieval.search import ensure_indexes, hybrid_search, keyword_search, lucene_escape, vector_search

DIMS = 1024


def fake_embed_api(dims=DIMS, seen=None):
    def handler(request):
        body = json.loads(request.content)
        if seen is not None:
            seen.extend(body["input"])
        return httpx.Response(200, json={"embeddings": [[0.1] * dims for _ in body["input"]]})
    return httpx.MockTransport(handler)


# ------------------------------------------------------------- embedder (unit)
def test_query_gets_instruction_prefix_but_documents_do_not():
    seen = []
    e = OllamaEmbedder(model="mxbai-embed-large", dimensions=DIMS, url="http://x", transport=fake_embed_api(seen=seen))
    e.embed_documents(["passage one", "passage two"])
    e.embed_query("my question")
    assert seen[:2] == ["passage one", "passage two"]
    assert seen[2].startswith("Represent this sentence") and seen[2].endswith("my question")


def test_wrong_dimension_fails_loudly():
    e = OllamaEmbedder(model="m", dimensions=DIMS, url="http://x", transport=fake_embed_api(dims=768))
    with pytest.raises(RuntimeError, match="768 dims"):
        e.embed_documents(["x"])


def test_lucene_escape_neutralises_operators():
    assert lucene_escape('C++ AND (x:"y")') == r'C\+\+ and \(x\:\"y\"\)'


# ------------------------------------------------------- search (integration)
class OneHotEmbedder:
    """Stands in for the model: the query vector is a one-hot at position `i`."""
    model, dimensions = "fake-onehot", DIMS

    def __init__(self, i):
        self.i = i

    def embed_query(self, question):
        v = [0.0] * DIMS
        v[self.i] = 1.0
        return v


@pytest.fixture(scope="module")
def ctx():
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    db = get_settings().neo4j_database
    ensure_indexes(driver, db, DIMS)
    tag = f"zz-test-{uuid.uuid4().hex[:8]}"
    texts = {0: f"{tag} the zebracorn ingestion pipeline", 1: f"{tag} quokkaflux vector migration notes"}
    rows = []
    for i, text in texts.items():
        v = [0.0] * DIMS
        v[1000 + i] = 1.0                                   # positions real embeddings won't peak at
        rows.append({"id": f"{tag}-c{i}", "text": text, "index": i, "v": v})
    driver.execute_query(
        """
        CREATE (d:Document {id: $tag + '-doc', filename: $tag + '.txt'})
        WITH d UNWIND $rows AS row
        CREATE (d)-[:HAS_CHUNK]->(c:Chunk {id: row.id, text: row.text, page: 1, chunkIndex: row.index})
        WITH c, row CALL db.create.setNodeVectorProperty(c, 'embedding', row.v)
        """, tag=tag, rows=rows, database_=db)
    driver.execute_query("CALL db.awaitIndexes(60)", database_=db)
    yield driver, db, tag
    driver.execute_query("MATCH (d:Document {id: $id})-[:HAS_CHUNK]->(c) DETACH DELETE d, c",
                         id=f"{tag}-doc", database_=db)
    close_driver()


def test_vector_search_returns_nearest_chunk(ctx):
    driver, db, tag = ctx
    top = vector_search(driver, db, OneHotEmbedder(1001), "anything", k=1)
    assert top[0]["chunk_id"] == f"{tag}-c1" and top[0]["score"] > 0.99


def test_keyword_search_finds_exact_rare_word(ctx):
    driver, db, tag = ctx
    top = keyword_search(driver, db, "zebracorn", k=1)
    assert top[0]["chunk_id"] == f"{tag}-c0"


def test_hybrid_puts_chunk_liked_by_both_methods_first(ctx):
    driver, db, tag = ctx
    # keyword likes c1 (quokkaflux), vector likes c1 (one-hot 1001) -> c1 must win
    top = hybrid_search(driver, db, OneHotEmbedder(1001), "quokkaflux", k=2)
    assert top[0]["chunk_id"] == f"{tag}-c1"
