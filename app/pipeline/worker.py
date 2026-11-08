"""M9 — Background worker: claim a job, run it, record the outcome, repeat.

Runs as its OWN process (scripts/worker.py), never inside the API request —
an extraction can take minutes; an HTTP request should take milliseconds.
"""
import logging
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import trace_id_var
from app.pipeline import jobs
from app.pipeline.process import PermanentError, TransientError, process_document

log = logging.getLogger("kg.worker")


def run_one(driver, database: str, worker_id: str, llm, embedder, extractions_dir: str = "extractions",
            only_ids: list[str] | None = None) -> dict | None:
    """Process at most one job. Returns {'id', 'status', ...} or None if the queue is empty.
    `only_ids` restricts which jobs may be claimed (tests use it to never touch real jobs)."""
    s = get_settings()
    job = jobs.claim_next_job(driver, database, worker_id, only_ids)
    if job is None:
        return None
    doc_id = job["id"]
    token = trace_id_var.set(doc_id)              # every log line for this job carries the document id
    log.info("job_claimed", extra={"document": job.get("filename"), "attempt": job.get("attempts"),
                                   "worker": worker_id})
    t0 = time.perf_counter()
    try:
        summary = process_document(driver, database, job, llm, embedder, extractions_dir)
        log.info("job_completed", extra={"seconds": round(time.perf_counter() - t0, 1), **summary})
        return {"id": doc_id, "status": jobs.COMPLETED, **summary}
    except PermanentError as exc:
        jobs.finish(driver, database, doc_id, exc.status, str(exc))
        log.warning("job_finished_permanent_error", extra={"status": exc.status, "error": str(exc)})
        return {"id": doc_id, "status": exc.status, "error": str(exc)}
    except TransientError as exc:
        status = jobs.schedule_retry(driver, database, doc_id, str(exc), job.get("attempts", 1),
                                     s.job_max_attempts, s.job_retry_base_seconds)
        log.warning("job_transient_error", extra={"status": status, "error": str(exc)})
        return {"id": doc_id, "status": status, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a bug must not kill the worker; record it and move on
        log.exception("job_crashed")
        jobs.finish(driver, database, doc_id, jobs.FAILED, f"unexpected {type(exc).__name__}: {exc}")
        return {"id": doc_id, "status": jobs.FAILED, "error": str(exc)}
    finally:
        trace_id_var.reset(token)


HEARTBEAT = Path("/tmp/kg-worker-heartbeat")   # Docker's healthcheck reads this file's age


def run_forever(driver, database: str, llm, embedder, poll_seconds: float = 2.0) -> None:
    s = get_settings()
    worker_id = jobs.new_worker_id()
    log.info("worker_started", extra={"worker": worker_id, "queue": s.job_queue, "poll_seconds": poll_seconds})
    last_recovery = 0.0
    while True:
        HEARTBEAT.touch()     # NOTE: a long LLM call pauses this; the healthcheck allows for that
        try:
            if time.monotonic() - last_recovery > 60:
                if n := jobs.recover_stale_jobs(driver, database, s.job_stale_after_minutes):
                    log.warning("stale_jobs_requeued", extra={"count": n})
                last_recovery = time.monotonic()
            did = run_one(driver, database, worker_id, llm, embedder)
        except Exception:  # noqa: BLE001 — e.g. Neo4j down while claiming: wait and try again
            log.exception("claim_failed_retrying")
            time.sleep(10)
            continue
        if did is None:
            time.sleep(poll_seconds)
