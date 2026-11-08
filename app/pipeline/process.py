"""M9 — The whole M3→M7 pipeline for ONE document, as a job the worker runs.

  chunking      parse + chunk + write Document/Chunk nodes             (M3)
  extracting    LLM per chunk, raw output saved to extractions/        (M4)
  writing_graph validate + resolve + write facts with provenance       (M5/M6)
  indexing      embed this document's chunks                           (M7)

Failure policy (guide §2.2):
  * file invalid               -> rejected          (final; retrying won't help)
  * no text in the file        -> extraction_failed (final; e.g. scanned PDF needs OCR)
  * one chunk's LLM output bad -> that chunk is skipped and recorded; the rest continue
  * Neo4j / Ollama unreachable -> raised as TransientError; the worker retries with backoff
Every stage is idempotent, so a retry simply starts over from the top.
"""
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError as Neo4jTransient

from app.extraction.llm_client import ExtractionFailed, OllamaClient
from app.extraction.prompts import PROMPT_VERSION
from app.extraction.validator import validate_extraction
from app.graph.graph_writer import write_document_graph
from app.graph.repository import upsert_document_with_chunks
from app.ingestion.chunker import chunk_pages
from app.ingestion.file_validator import InvalidDocument, validate_file
from app.ingestion.parsers import parse_file
from app.pipeline import jobs
from app.retrieval.embedder import OllamaEmbedder
from app.retrieval.search import embed_missing_chunks
from app.schemas.extraction import SCHEMA_VERSION

MAX_CHARS, OVERLAP = 600, 100
TRANSIENT = (httpx.TransportError, ServiceUnavailable, SessionExpired, Neo4jTransient)
log = logging.getLogger("kg.pipeline")


@contextmanager
def _stage(driver, db, doc_id: str, name: str, timings: dict):
    """Mark the stage in Neo4j (visible via the API) and log how long it took."""
    jobs.set_stage(driver, db, doc_id, name)
    t0 = time.perf_counter()
    yield
    timings[name] = round(time.perf_counter() - t0, 2)
    log.info("stage_done", extra={"stage": name, "seconds": timings[name]})


class TransientError(RuntimeError):
    """Worth retrying later (a dependency is down or slow)."""


class PermanentError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def process_document(driver, database: str, job: dict, llm: OllamaClient, embedder: OllamaEmbedder,
                     extractions_dir: str = "extractions") -> dict:
    try:
        return _run(driver, database, job, llm, embedder, Path(extractions_dir))
    except TRANSIENT as exc:
        raise TransientError(f"{type(exc).__name__}: {exc}") from exc


def _run(driver, db, job, llm, embedder, out_dir: Path) -> dict:
    doc_id = job["id"]
    timings: dict[str, float] = {}

    # ---- chunking (M3)
    with _stage(driver, db, doc_id, "chunking", timings):
        try:
            info = validate_file(job["storedPath"])
        except InvalidDocument as exc:
            raise PermanentError(jobs.REJECTED, str(exc)) from exc
        if info.id != doc_id:
            raise PermanentError(jobs.REJECTED, "stored file no longer matches the uploaded bytes")
        pages = parse_file(info.path)
        if not pages:
            raise PermanentError(jobs.EXTRACTION_FAILED, "no extractable text (a scanned PDF needs OCR)")
        chunks = chunk_pages(doc_id, pages, MAX_CHARS, OVERLAP)
        doc = {"id": doc_id, "filename": job.get("filename", info.filename), "sha256": info.sha256,
               "size_bytes": info.size_bytes, "status": jobs.PROCESSING}
        with driver.session(database=db) as s:
            s.execute_write(upsert_document_with_chunks, doc, [asdict(c) for c in chunks])

    # ---- extracting (M4) — the slow part; progress is visible through the API
    results, reports, failed = [], [], 0
    with _stage(driver, db, doc_id, "extracting", timings):
        for n, c in enumerate(chunks, 1):
            jobs.set_stage(driver, db, doc_id, "extracting", f"{n}/{len(chunks)} chunks")
            try:
                llm_result = llm.extract(c.text)
            except ExtractionFailed as exc:
                failed += 1
                log.warning("chunk_extraction_failed", extra={"chunk_id": c.id, "attempts": len(exc.raw_attempts)})
                results.append({"chunk_id": c.id, "status": "failed", "raw_attempts": exc.raw_attempts})
                continue
            report = validate_extraction(llm_result.extraction, c.text, c.id)
            reports.append(report)
            # guide §7.8 / §12.6: model, prompt version, tokens, attempts — never the text itself
            log.info("chunk_extracted", extra={
                "chunk_id": c.id, "model": llm.model, "prompt_version": PROMPT_VERSION,
                "attempts": llm_result.attempts, "prompt_tokens": llm_result.prompt_tokens,
                "output_tokens": llm_result.output_tokens, "llm_seconds": round(llm_result.seconds, 1),
                "accepted": len(report.relationships), "review": len(report.needs_review),
                "rejected": len(report.rejected)})
            results.append({"chunk_id": c.id, "chunk_index": c.index, "page": c.page, "status": "ok",
                            "attempts": llm_result.attempts, "raw": llm_result.raw, **asdict(report)})
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{doc_id}.{PROMPT_VERSION}.{llm.model.replace(':', '_')}.json").write_text(json.dumps({
        "document_id": doc_id, "model": llm.model, "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION, "max_chars": MAX_CHARS, "overlap": OVERLAP,
        "source_path": job["storedPath"], "created_at": jobs.utcnow(), "chunks": results,
    }, indent=2, ensure_ascii=False))

    # ---- writing_graph (M5/M6)
    with _stage(driver, db, doc_id, "writing_graph", timings):
        with driver.session(database=db) as s:
            stats = s.execute_write(write_document_graph, doc, [asdict(c) for c in chunks], reports,
                                    llm.model, PROMPT_VERSION)

    # ---- indexing (M7)
    with _stage(driver, db, doc_id, "indexing", timings):
        embedded = embed_missing_chunks(driver, db, embedder, document_id=doc_id)

    summary = {"chunks": len(chunks), "failedChunks": failed, "factsWritten": stats.relationships_written,
               "reviewItems": stats.review_items, "embedded": embedded,
               "stageSeconds": json.dumps(timings)}       # Neo4j properties can't be maps -> JSON string
    jobs.finish(driver, db, doc_id, jobs.COMPLETED,
                error=f"{failed} chunk(s) failed extraction" if failed else None, **summary)
    return summary
