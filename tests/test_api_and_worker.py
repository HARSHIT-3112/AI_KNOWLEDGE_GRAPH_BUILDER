"""M9 tests: API contract + worker state machine, against the real Neo4j (skipped if it's down).

The LLM and embedder are FAKES, so this runs in seconds. Uploads go to a temp
folder, documents are uniquely tagged, and the worker may only claim the test's
own job ids — your real data and queue are never touched.
"""
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.core.config import get_settings
from app.db.neo4j import get_driver
from app.extraction.llm_client import OllamaClient
from app.pipeline import jobs
from app.pipeline.worker import run_one

TAG = f"zz-test-{uuid.uuid4().hex[:8]}"
TEXT = f"{TAG}Person works at {TAG}Corp as a tester. {TAG}Person works on {TAG}Proj."


class FakeEmbedder:
    model, dimensions = "fake-test-embedder", 1024

    def embed_documents(self, texts):
        return [[0.0] * 1023 + [1.0] for _ in texts]


def fake_llm(fail: bool = False) -> OllamaClient:
    reply = {"entities": [{"name": f"{TAG}Person", "type": "Person"}, {"name": f"{TAG}Corp", "type": "Company"}],
             "relationships": [{"source": f"{TAG}Person", "type": "WORKS_AT", "target": f"{TAG}Corp",
                                "properties": {"role": "tester", "since": None},
                                "evidence": f"{TAG}Person works at {TAG}Corp as a tester"}]}

    def handler(request):
        if fail:
            raise httpx.ConnectError("ollama is down", request=request)
        return httpx.Response(200, json={"message": {"content": json.dumps(reply)}})
    return OllamaClient(model="fake", url="http://x", max_attempts=1, transport=httpx.MockTransport(handler))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    try:
        get_driver().verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    s = get_settings()
    old_dir, old_queue = s.upload_dir, s.job_queue
    s.upload_dir = str(tmp_path_factory.mktemp("uploads"))
    # Private queue: a real worker running on 'default' must never pick up test jobs.
    # (Found the hard way: a live worker claimed a test upload and ran the real LLM on it.)
    s.job_queue = f"test-{TAG}"
    with TestClient(app) as c:
        c.headers["X-API-Key"] = s.api_key
        yield c
    s.upload_dir, s.job_queue = old_dir, old_queue
    driver = get_driver()
    driver.execute_query(
        """
        MATCH (d:Document) WHERE d.filename STARTS WITH 'zz-test-'
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) OPTIONAL MATCH (c)<-[:FROM_CHUNK]-(ri)
        DETACH DELETE d, c, ri
        """, database_=s.neo4j_database)
    driver.execute_query("MATCH (e:Entity) WHERE e.name STARTS WITH $t DETACH DELETE e", t=TAG,
                         database_=s.neo4j_database)


def upload(client, content: str | bytes, name: str | None = None):
    data = content.encode() if isinstance(content, str) else content
    return client.post("/documents", files={"file": (name or f"{TAG}.txt", data, "text/plain")})


def run_worker(doc_id, llm=None):
    s = get_settings()
    return run_one(get_driver(), s.neo4j_database, "test-worker", llm or fake_llm(), FakeEmbedder(),
                   extractions_dir=s.upload_dir, only_ids=[doc_id])


# ---------------------------------------------------------------- API rules
def test_requests_without_api_key_are_rejected(client):
    r = client.get("/documents", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_unsupported_and_fake_files_rejected(client):
    assert upload(client, b"MZ...", f"{TAG}.exe").status_code == 400
    assert upload(client, b"not a pdf", f"{TAG}.pdf").status_code == 400


def test_too_large_upload_rejected(client):
    s = get_settings()
    old, s.max_upload_bytes = s.max_upload_bytes, 100
    try:
        assert upload(client, "x" * 500).status_code == 413
    finally:
        s.max_upload_bytes = old


def test_unknown_document_is_404(client):
    assert client.get("/documents/doc-doesnotexist").status_code == 404


# -------------------------------------------------- full job lifecycle
def test_upload_process_and_status(client):
    r = upload(client, TEXT)
    assert r.status_code == 202 and r.json()["status"] == jobs.RECEIVED
    doc_id = r.json()["document_id"]

    again = upload(client, TEXT, f"{TAG}-renamed.txt")         # same bytes, different name
    assert again.status_code == 200 and again.json()["duplicate"] and again.json()["document_id"] == doc_id

    result = run_worker(doc_id)
    assert result["status"] == jobs.COMPLETED

    doc = client.get(f"/documents/{doc_id}").json()
    assert doc["status"] == "completed" and doc["attempts"] == 1
    assert doc["chunks"] == 1 and doc["embedded_chunks"] == 1 and doc["facts"] == 1

    assert run_worker(doc_id) is None                           # nothing left to claim


def test_job_is_claimed_only_once(client):
    doc_id = upload(client, TEXT + " claim-once").json()["document_id"]
    s = get_settings()
    first = jobs.claim_next_job(get_driver(), s.neo4j_database, "w1", [doc_id])
    second = jobs.claim_next_job(get_driver(), s.neo4j_database, "w2", [doc_id])
    assert first["id"] == doc_id and first["lockedBy"] == "w1" and second is None
    jobs.finish(get_driver(), s.neo4j_database, doc_id, jobs.FAILED, "test")


def test_ollama_down_is_retried_then_fails(client):
    s = get_settings()
    doc_id = upload(client, TEXT + " flaky").json()["document_id"]
    old = s.job_retry_base_seconds
    s.job_retry_base_seconds = 0                                 # no waiting in tests
    try:
        statuses = [run_worker(doc_id, fake_llm(fail=True))["status"] for _ in range(s.job_max_attempts)]
    finally:
        s.job_retry_base_seconds = old
    assert statuses == [jobs.RETRY] * (s.job_max_attempts - 1) + [jobs.FAILED]
    doc = client.get(f"/documents/{doc_id}").json()
    assert doc["attempts"] == s.job_max_attempts and "gave up" in doc["error"]

    # reprocess puts it back in the queue; with Ollama "back up" it completes
    assert client.post(f"/documents/{doc_id}/reprocess").json()["status"] == jobs.RECEIVED
    assert run_worker(doc_id)["status"] == jobs.COMPLETED


def test_empty_text_file_is_permanent_failure(client):
    doc_id = upload(client, "   \n\n   ").json()["document_id"]
    assert run_worker(doc_id)["status"] == jobs.EXTRACTION_FAILED


def test_delete_removes_document_and_its_facts(client):
    doc_id = upload(client, TEXT + " to-delete").json()["document_id"]
    run_worker(doc_id)
    r = client.delete(f"/documents/{doc_id}")
    assert r.status_code == 200
    assert client.get(f"/documents/{doc_id}").status_code == 404
