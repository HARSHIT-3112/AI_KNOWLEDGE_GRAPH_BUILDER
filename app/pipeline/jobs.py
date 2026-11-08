"""M9 — A durable job queue stored IN Neo4j (guide §10.3 stage C, §10.4 state machine).

The (:Document) node IS the job. Its `status` walks this state machine:

  received ──claim──► processing: chunking → extracting → writing_graph → indexing ──► completed
      ▲                    │
      │  transient error   ├─ no text in file          ──► extraction_failed   (final)
      └── retry (backoff) ◄┤  (Neo4j/Ollama down,      ──► rejected            (final, invalid file)
                           │   timeouts)                   failed              (final, after N attempts)

Why Neo4j and not Redis/Celery? No extra service to run on an 8 GB laptop, and
the job state survives restarts. The trade-off: polling instead of push, and
it won't scale to many workers. Redis + RQ/Celery is the production upgrade —
the worker code barely changes, only claim_next_job does.
"""
import secrets
from datetime import datetime, timezone

from app.core.config import get_settings

RECEIVED, PROCESSING, RETRY = "received", "processing", "retry"
COMPLETED, FAILED, REJECTED, EXTRACTION_FAILED = "completed", "failed", "rejected", "extraction_failed"
FINAL_STATES = {COMPLETED, FAILED, REJECTED, EXTRACTION_FAILED}
STAGES = ["chunking", "extracting", "writing_graph", "indexing"]


def new_worker_id() -> str:
    return f"worker-{secrets.token_hex(3)}"


def enqueue_document(driver, database: str, doc: dict) -> tuple[dict, bool]:
    """Create the job, or return the existing one (same bytes = same id). Returns (doc, created)."""
    rec = driver.execute_query(
        """
        MERGE (d:Document {id: $doc.id})
        ON CREATE SET d.createdAt = datetime(), d.status = $received, d.attempts = 0, d.queue = $queue,
                      d.filename = $doc.filename, d.sha256 = $doc.sha256, d.sizeBytes = $doc.size_bytes,
                      d.storedPath = $doc.stored_path, d._created = true
        WITH d, coalesce(d._created, false) AS created
        REMOVE d._created
        RETURN d {.*} AS doc, created
        """,
        doc=doc, received=RECEIVED, queue=get_settings().job_queue, database_=database,
    ).records[0]
    return _clean(rec["doc"]), rec["created"]


def requeue(driver, database: str, doc_id: str) -> dict | None:
    """Reprocess: put any non-running job back in the queue."""
    rec = driver.execute_query(
        """
        MATCH (d:Document {id: $id}) WHERE d.status <> $processing
        SET d.status = $received, d.attempts = 0, d.error = null, d.stage = null,
            d.nextAttemptAt = null, d.progress = null
        RETURN d {.*} AS doc
        """,
        id=doc_id, processing=PROCESSING, received=RECEIVED, database_=database,
    ).records
    return _clean(rec[0]["doc"]) if rec else None


def claim_next_job(driver, database: str, worker_id: str, only_ids: list[str] | None = None) -> dict | None:
    """Atomically take ONE waiting job, so two workers never process the same document.

    The `SET d._lock = true` FIRST is the important trick: it takes the node's write lock,
    and only THEN do we re-check the status. Without it, two workers can both read
    status='received' and both "claim" it (a lost update)."""
    def tx_fn(tx):
        candidate = tx.run(
            """
            MATCH (d:Document)
            WHERE (d.status = $received OR (d.status = $retry AND d.nextAttemptAt <= datetime()))
              AND coalesce(d.queue, 'default') = $queue
              AND ($only IS NULL OR d.id IN $only)
            RETURN d.id AS id ORDER BY d.createdAt LIMIT 1
            """,
            received=RECEIVED, retry=RETRY, only=only_ids, queue=get_settings().job_queue,
        ).single()
        if candidate is None:
            return None
        rec = tx.run(
            """
            MATCH (d:Document {id: $id})
            SET d._lock = true                                   // take the write lock
            WITH d WHERE d.status = $received OR (d.status = $retry AND d.nextAttemptAt <= datetime())
            SET d.status = $processing, d.lockedBy = $worker, d.lockedAt = datetime(),
                d.attempts = coalesce(d.attempts, 0) + 1, d.startedAt = datetime()
            REMOVE d._lock
            RETURN d {.*} AS doc
            """,
            id=candidate["id"], received=RECEIVED, retry=RETRY, processing=PROCESSING, worker=worker_id,
        ).single()
        tx.run("MATCH (d:Document {id: $id}) REMOVE d._lock", id=candidate["id"])
        return _clean(rec["doc"]) if rec else None

    with driver.session(database=database) as s:
        return s.execute_write(tx_fn)


def set_stage(driver, database: str, doc_id: str, stage: str, progress: str | None = None) -> None:
    driver.execute_query(
        "MATCH (d:Document {id: $id}) SET d.stage = $stage, d.progress = $progress, d.lockedAt = datetime()",
        id=doc_id, stage=stage, progress=progress, database_=database,
    )


def finish(driver, database: str, doc_id: str, status: str, error: str | None = None, **extra) -> None:
    driver.execute_query(
        """
        MATCH (d:Document {id: $id})
        SET d.status = $status, d.error = $error, d.finishedAt = datetime(), d.lockedBy = null,
            d.stage = null, d.progress = null, d += $extra
        """,
        id=doc_id, status=status, error=error, extra=extra, database_=database,
    )


def schedule_retry(driver, database: str, doc_id: str, error: str, attempts: int,
                   max_attempts: int, base_seconds: int) -> str:
    """Exponential backoff; give up after max_attempts. Returns the new status."""
    if attempts >= max_attempts:
        finish(driver, database, doc_id, FAILED, f"gave up after {attempts} attempts: {error}")
        return FAILED
    delay = base_seconds * 2 ** (attempts - 1)
    driver.execute_query(
        """
        MATCH (d:Document {id: $id})
        SET d.status = $retry, d.error = $error, d.lockedBy = null, d.stage = null,
            d.nextAttemptAt = datetime() + duration({seconds: $delay})
        """,
        id=doc_id, retry=RETRY, error=error, delay=delay, database_=database,
    )
    return RETRY


def recover_stale_jobs(driver, database: str, stale_minutes: int) -> int:
    """A worker that crashed mid-job leaves status=processing forever. Put those back."""
    return driver.execute_query(
        """
        MATCH (d:Document {status: $processing})
        WHERE d.lockedAt < datetime() - duration({minutes: $m})
        SET d.status = $retry, d.nextAttemptAt = datetime(), d.lockedBy = null,
            d.error = 'worker stopped responding; requeued'
        RETURN count(d) AS n
        """,
        processing=PROCESSING, retry=RETRY, m=stale_minutes, database_=database,
    ).records[0]["n"]


def get_document(driver, database: str, doc_id: str) -> dict | None:
    rec = driver.execute_query(
        """
        MATCH (d:Document {id: $id})
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
        WITH d, count(c) AS chunks, count(c.embedding) AS embedded
        OPTIONAL MATCH ()-[r]->() WHERE d.id IN coalesce(r.sourceDocIds, [])
        WITH d, chunks, embedded, count(r) AS facts
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->()<-[:FROM_CHUNK]-(ri:ReviewItem {status: 'pending'})
        RETURN d {.*} AS doc, chunks, embedded, facts, count(ri) AS pending_review
        """,
        id=doc_id, database_=database,
    ).records
    if not rec:
        return None
    r = rec[0]
    return {**_clean(r["doc"]), "chunks": r["chunks"], "embedded_chunks": r["embedded"],
            "facts": r["facts"], "pending_review": r["pending_review"]}


def list_documents(driver, database: str, limit: int = 50) -> list[dict]:
    records = driver.execute_query(
        "MATCH (d:Document) RETURN d {.*} AS doc ORDER BY d.createdAt DESC LIMIT $limit",
        limit=limit, database_=database,
    ).records
    return [_clean(r["doc"]) for r in records]


def _clean(props: dict) -> dict:
    """Neo4j temporal types -> ISO strings so they serialise as JSON."""
    out = {}
    for k, v in props.items():
        if k.startswith("_"):
            continue
        out[k] = v.iso_format() if hasattr(v, "iso_format") else v
    return out


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
