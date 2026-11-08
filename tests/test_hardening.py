"""M10 tests: JSON logs, trace ids, rate limits, liveness vs readiness, metrics."""
import json
import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.ratelimit import TokenBucket, limit
from app.core.logging import JsonFormatter, trace_id_var


# ------------------------------------------------------------------ logging
def test_log_lines_are_json_with_trace_id_and_extras():
    rec = logging.LogRecord("kg.test", logging.INFO, __file__, 1, "stage_done", None, None)
    rec.stage, rec.seconds = "extracting", 1.5
    token = trace_id_var.set("doc-123")
    try:
        line = json.loads(JsonFormatter().format(rec))
    finally:
        trace_id_var.reset(token)
    assert line["msg"] == "stage_done" and line["trace_id"] == "doc-123"
    assert line["stage"] == "extracting" and line["seconds"] == 1.5 and line["level"] == "INFO"


# --------------------------------------------------------------- rate limit
def test_token_bucket_allows_burst_then_blocks():
    b = TokenBucket(per_minute=60, capacity=3)
    assert [b.take("k") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.take("k") > 0                         # 4th request in the same instant is refused
    assert b.take("other-key") == 0.0               # buckets are per client


def test_rate_limited_endpoint_returns_429_with_retry_after():
    mini = FastAPI()

    @mini.get("/x", dependencies=[Depends(limit(TokenBucket(per_minute=2, capacity=2), "x"))])
    def x():
        return {"ok": True}

    c = TestClient(mini)
    assert [c.get("/x").status_code for _ in range(3)] == [200, 200, 429]
    r = c.get("/x")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


# ------------------------------------------------------- API (needs Neo4j)
@pytest.fixture(scope="module")
def client():
    from app.api.main import app
    from app.core.config import get_settings
    from app.db.neo4j import get_driver
    try:
        get_driver().verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j not reachable: {exc}")
    with TestClient(app) as c:
        c.headers["X-API-Key"] = get_settings().api_key
        yield c


def test_liveness_needs_no_auth_and_no_dependencies(client):
    r = client.get("/health/live", headers={"X-API-Key": ""})
    assert r.status_code == 200 and r.json() == {"status": "alive"}


def test_request_id_is_echoed_and_sanitised(client):
    assert client.get("/health/live", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"
    evil = client.get("/health/live", headers={"X-Request-ID": 'x"\n{"level":"FAKE"}'}).headers["X-Request-ID"]
    assert evil == "xlevelFAKE"                     # no quotes/newlines can be smuggled into logs
    assert len(client.get("/health/live").headers["X-Request-ID"]) == 16   # generated when absent


def test_metrics_shape(client):
    m = client.get("/metrics").json()
    assert isinstance(m["documents_by_status"], dict)
    assert {"entities", "facts_with_evidence", "chunks", "chunks_embedded"} <= set(m["graph"])
    assert client.get("/metrics", headers={"X-API-Key": "nope"}).status_code == 401
