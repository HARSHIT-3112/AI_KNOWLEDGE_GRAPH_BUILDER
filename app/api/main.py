"""M9 — HTTP API (guide §10.5, §10.6).

Rules this file follows:
  * Requests are FAST: upload only stores the file and enqueues a job (202 Accepted).
    The worker (separate process) does the slow pipeline.
  * Every endpoint except /health needs the X-API-Key header (guide §12.4).
  * Uploads are size-limited while streaming, validated, and stored under a
    server-chosen name (never the client's filename -> no path traversal).
  * Same bytes uploaded twice -> same document id -> 200 with the existing job (idempotent).

Run:  .venv/bin/uvicorn app.api.main:app --port 8000      then open http://localhost:8000/docs
"""
import logging
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from app.api.ratelimit import TokenBucket, limit
from app.core.config import get_settings
from app.core.logging import setup_logging, trace_id_var
from app.db.neo4j import close_driver, get_driver
from app.extraction.llm_client import OllamaClient
from app.graph.graph_writer import delete_document
from app.ingestion.file_validator import ALLOWED_SUFFIXES, InvalidDocument, validate_file
from app.pipeline import jobs
from app.retrieval.answerer import answer_question
from app.retrieval.embedder import OllamaEmbedder
from app.retrieval.graph_retriever import neighbourhood_facts
from app.retrieval.search import hybrid_search, keyword_search, vector_search


log = logging.getLogger("kg.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.driver = get_driver()                 # one shared driver for the whole app (guide §5.1)
    app.state.embedder = OllamaEmbedder()
    app.state.llm = OllamaClient()
    yield
    app.state.embedder.close()
    app.state.llm.close()
    close_driver()


app = FastAPI(title="AI Knowledge Graph Builder", version="1.0.0", lifespan=lifespan)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    """M10: one trace id per request — taken from X-Request-ID if the caller sent one — on every
    log line and echoed back, so a user's bug report can be matched to the server logs."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    rid = "".join(ch for ch in rid if ch.isalnum() or ch in "-_")[:64] or uuid.uuid4().hex[:16]  # untrusted input
    token = trace_id_var.set(rid)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        log.info("request", extra={"method": request.method, "path": request.url.path,
                                   "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000)})
        return response
    except Exception:
        log.exception("request_crashed", extra={"method": request.method, "path": request.url.path})
        raise
    finally:
        trace_id_var.reset(token)


def require_api_key(key: str | None = Depends(_api_key_header)) -> None:
    if not key or not secrets.compare_digest(key, get_settings().api_key):   # constant-time compare
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")


def db() -> str:
    return get_settings().neo4j_database


Auth = Depends(require_api_key)
_s = get_settings()
AskLimit = Depends(limit(TokenBucket(_s.rate_ask_per_minute), "ask"))
UploadLimit = Depends(limit(TokenBucket(_s.rate_upload_per_minute), "upload"))
ReadLimit = Depends(limit(TokenBucket(_s.rate_read_per_minute), "read"))


# ----------------------------------------------------------------- health
@app.get("/health/live")
def live():
    """Liveness: the process is up and serving. No dependency checks — if Neo4j is down,
    restarting the API won't fix it, so the orchestrator must NOT restart us for that."""
    return {"status": "alive"}


@app.get("/health/ready")
@app.get("/health")
def health(request: Request):
    """Readiness: can we actually serve requests? (Neo4j + Ollama reachable)"""
    out = {"neo4j": "ok", "ollama": "ok"}
    try:
        request.app.state.driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        out["neo4j"] = f"down: {type(exc).__name__}"
    try:
        request.app.state.llm.ensure_model()
    except Exception as exc:  # noqa: BLE001
        out["ollama"] = f"down: {type(exc).__name__}"
    if "down" in str(out):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, out)
    return out


# -------------------------------------------------------------- documents
@app.post("/documents", status_code=status.HTTP_202_ACCEPTED, dependencies=[Auth, UploadLimit])
async def upload_document(request: Request, response: Response, file: UploadFile = File(...)):
    s = get_settings()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported file type; allowed: {sorted(ALLOWED_SUFFIXES)}")

    upload_dir = Path(s.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    tmp = upload_dir / f".incoming-{uuid.uuid4().hex}{suffix}"
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):        # stream: never hold a huge file in memory
                size += len(chunk)
                if size > s.max_upload_bytes:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE,
                                        f"file larger than {s.max_upload_bytes} bytes")
                out.write(chunk)
        try:
            info = validate_file(tmp)
        except InvalidDocument as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        stored = upload_dir / f"{info.id}{suffix}"
        if not stored.exists():
            shutil.move(tmp, stored)
    finally:
        tmp.unlink(missing_ok=True)

    doc, created = jobs.enqueue_document(request.app.state.driver, db(), {
        "id": info.id, "filename": Path(file.filename or stored.name).name, "sha256": info.sha256,
        "size_bytes": info.size_bytes, "stored_path": str(stored)})
    if not created:
        response.status_code = status.HTTP_200_OK
    return {"document_id": info.id, "status": doc.get("status"), "duplicate": not created,
            "status_url": f"/documents/{info.id}"}


@app.get("/documents", dependencies=[Auth, ReadLimit])
def list_docs(request: Request, limit: int = Query(50, ge=1, le=500)):
    return jobs.list_documents(request.app.state.driver, db(), limit)


@app.get("/documents/{doc_id}", dependencies=[Auth, ReadLimit])
def get_doc(request: Request, doc_id: str):
    doc = jobs.get_document(request.app.state.driver, db(), doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


@app.post("/documents/{doc_id}/reprocess", status_code=status.HTTP_202_ACCEPTED, dependencies=[Auth, UploadLimit])
def reprocess(request: Request, doc_id: str):
    doc = jobs.get_document(request.app.state.driver, db(), doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if not doc.get("storedPath"):
        raise HTTPException(status.HTTP_409_CONFLICT, "document was not uploaded through the API; re-upload it")
    requeued = jobs.requeue(request.app.state.driver, db(), doc_id)
    if requeued is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "document is being processed right now")
    return {"document_id": doc_id, "status": requeued["status"]}


@app.delete("/documents/{doc_id}", dependencies=[Auth, UploadLimit])
def delete_doc(request: Request, doc_id: str):
    driver = request.app.state.driver
    doc = jobs.get_document(driver, db(), doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if doc.get("status") == jobs.PROCESSING:
        raise HTTPException(status.HTTP_409_CONFLICT, "document is being processed right now")
    with driver.session(database=db()) as session:
        stats = session.execute_write(delete_document, doc_id)
    if doc.get("storedPath"):
        Path(doc["storedPath"]).unlink(missing_ok=True)
    return {"deleted": doc_id, **stats}


# --------------------------------------------------------- search & ask
@app.get("/search", dependencies=[Auth, ReadLimit])
def search(request: Request, q: str = Query(..., min_length=2, max_length=500),
           k: int = Query(5, ge=1, le=20), mode: Literal["hybrid", "vector", "keyword"] = "hybrid"):
    st = request.app.state
    if mode == "keyword":
        rows = keyword_search(st.driver, db(), q, k)
    elif mode == "vector":
        rows = vector_search(st.driver, db(), st.embedder, q, k)
    else:
        rows = hybrid_search(st.driver, db(), st.embedder, q, k)
    return [{**r, "text": r["text"][:500]} for r in rows]


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.post("/ask", dependencies=[Auth, AskLimit])
def ask(request: Request, body: AskRequest):
    # plain `def` -> FastAPI runs it in a thread pool, so a slow LLM call doesn't block other requests
    st = request.app.state
    r = answer_question(st.driver, db(), body.question, st.embedder, st.llm)
    return {
        "question": r.question, "answer": r.answer, "grounded": r.grounded,
        "abstained_reason": r.abstained_reason,
        "citations": [{"id": c.id, "kind": c.kind, "document": c.document, "page": c.page,
                       "text": c.text[:300]} for c in r.citations],
        "warnings": {"invalid_citations": r.invalid_citations, "unsupported_terms": r.unsupported_terms},
        "linked_entities": [e.name for e in r.linked_entities],
        "top_vector_score": round(r.top_vector_score, 3),
    }


# -------------------------------------------------------- graph & review
@app.get("/entities/{entity_id}", dependencies=[Auth, ReadLimit])
def entity(request: Request, entity_id: str):
    driver = request.app.state.driver
    rec = driver.execute_query(
        "MATCH (e:Entity {id: $id}) RETURN e.name AS name, e.type AS type, coalesce(e.aliases, []) AS aliases",
        id=entity_id, database_=db()).records
    if not rec:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity not found")
    facts = neighbourhood_facts(driver, db(), [entity_id], max_hops=1, limit=50)
    return {"id": entity_id, **dict(rec[0]),
            "facts": [{"fact": f.sentence(), "evidence": f.evidence} for f in facts]}


@app.get("/review", dependencies=[Auth, ReadLimit])
def review_queue(request: Request, limit: int = Query(50, ge=1, le=500)):
    records = request.app.state.driver.execute_query(
        """
        MATCH (ri:ReviewItem {status: 'pending'})-[:FROM_CHUNK]->(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
        RETURN ri.id AS id, ri.reason AS reason, ri.payload AS payload, d.filename AS document, c.page AS page
        ORDER BY ri.createdAt DESC LIMIT $limit
        """, limit=limit, database_=db()).records
    return [dict(r) for r in records]


# ------------------------------------------------------------- metrics (M10)
@app.get("/metrics", dependencies=[Auth, ReadLimit])
def metrics(request: Request):
    """Operational counters (guide §12.6): documents by status, stuck/failed jobs, review backlog,
    graph size. Poll this from a dashboard or alerting job."""
    r = request.app.state.driver.execute_query(
        """
        CALL () { MATCH (d:Document) RETURN collect(coalesce(d.status, 'unknown')) AS statuses }
        CALL () { MATCH (d:Document {status: 'retry'}) RETURN count(d) AS retrying }
        CALL () { MATCH (ri:ReviewItem {status: 'pending'}) RETURN count(ri) AS review_pending }
        CALL () { MATCH (e:Entity) RETURN count(e) AS entities }
        CALL () { MATCH ()-[r]->() WHERE size(coalesce(r.sourceDocIds, [])) > 0 RETURN count(r) AS facts }
        CALL () { MATCH (c:Chunk) RETURN count(c) AS chunks, count(c.embedding) AS embedded }
        CALL () { MATCH (d:Document {status: 'processing'}) RETURN min(d.lockedAt) AS oldest_processing }
        RETURN statuses, retrying, review_pending, entities, facts, chunks, embedded, oldest_processing
        """, database_=db()).records[0]
    by_status: dict[str, int] = {}
    for st in r["statuses"]:
        by_status[st] = by_status.get(st, 0) + 1
    oldest = r["oldest_processing"]
    return {"documents_by_status": by_status, "review_pending": r["review_pending"],
            "graph": {"entities": r["entities"], "facts_with_evidence": r["facts"],
                      "chunks": r["chunks"], "chunks_embedded": r["embedded"]},
            "oldest_processing_since": oldest.iso_format() if oldest else None}
