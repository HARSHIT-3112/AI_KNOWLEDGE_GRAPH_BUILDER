"""M4: chunk -> LLM -> schema-valid JSON -> grounded validation. NO database writes yet.

Guide: "make extraction correct without writing to Neo4j. Only then connect the two."
Each run saves everything (raw model output, accepted facts, rejections, prompt
version, model, tokens) to extractions/ so you can inspect and compare runs.

Run:
  .venv/bin/python -m scripts.m4_extract documents/sample_company.txt
  .venv/bin/python -m scripts.m4_extract documents/sample_company.txt --chunk 1
  .venv/bin/python -m scripts.m4_extract --text "Asha works at Zeta as a designer."
"""
import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.extraction.llm_client import ExtractionFailed, OllamaClient
from app.extraction.prompts import PROMPT_VERSION
from app.extraction.validator import validate_extraction
from app.ingestion.chunker import Chunk, chunk_id, chunk_pages
from app.ingestion.file_validator import validate_file
from app.ingestion.parsers import parse_file
from app.schemas.extraction import SCHEMA_VERSION

OUT_DIR = Path("extractions")


def show(report) -> None:
    for e in report.entities:
        print(f"    entity   {e.type:<10} {e.name}")
    for r in report.relationships:
        props = f" {r.properties}" if r.properties else ""
        print(f"    ✓ ({r.source})-[:{r.type}{props}]->({r.target})   « {r.evidence} »")
    for mark, items in (("?", report.needs_review), ("✗", report.rejected)):
        for x in items:
            label = x.item.get("name") or f"({x.item.get('source')})-[:{x.item.get('type')}]->({x.item.get('target')})"
            print(f"    {mark} {label}  — {x.reason}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?")
    ap.add_argument("--text", help="extract from this string instead of a file")
    ap.add_argument("--chunk", type=int, help="only this chunk index")
    ap.add_argument("--max-chars", type=int, default=600)
    ap.add_argument("--overlap", type=int, default=100)
    ap.add_argument("--model")
    args = ap.parse_args()

    if args.text:
        doc_id, chunks = "doc-adhoc", [Chunk(chunk_id("doc-adhoc", 0, args.text), "doc-adhoc", 0, 1, args.text)]
    elif args.path:
        info = validate_file(args.path)
        doc_id, chunks = info.id, chunk_pages(info.id, parse_file(info.path), args.max_chars, args.overlap)
    else:
        ap.error("give a file path or --text")
    if args.chunk is not None:
        chunks = [c for c in chunks if c.index == args.chunk]

    client = OllamaClient(model=args.model)
    client.ensure_model()
    print(f"Model {client.model} | prompt {PROMPT_VERSION} | schema {SCHEMA_VERSION} | {len(chunks)} chunk(s)\n")

    results, totals = [], {"accepted_relationships": 0, "needs_review": 0, "rejected": 0, "failed_chunks": 0}
    try:
        for c in chunks:
            print(f"[chunk {c.index}] {c.id} (page {c.page})")
            try:
                llm = client.extract(c.text)
            except ExtractionFailed as exc:
                print(f"    !! {exc}")
                totals["failed_chunks"] += 1
                results.append({"chunk_id": c.id, "status": "failed", "raw_attempts": exc.raw_attempts})
                continue
            report = validate_extraction(llm.extraction, c.text, c.id)
            show(report)
            print(f"    ({llm.seconds:.1f}s, {llm.prompt_tokens}+{llm.output_tokens} tokens, attempt {llm.attempts})\n")
            totals["accepted_relationships"] += len(report.relationships)
            totals["rejected"] += len(report.rejected)
            totals["needs_review"] += len(report.needs_review)
            results.append({"chunk_id": c.id, "chunk_index": c.index, "page": c.page, "status": "ok",
                            "attempts": llm.attempts, "tokens": [llm.prompt_tokens, llm.output_tokens],
                            "raw": llm.raw, **asdict(report)})
    finally:
        client.close()

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{doc_id}.{PROMPT_VERSION}.{client.model.replace(':', '_')}.json"
    out.write_text(json.dumps({
        "document_id": doc_id, "model": client.model, "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION, "max_chars": args.max_chars, "overlap": args.overlap,
        "source_path": str(args.path) if args.path else None, "created_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals, "chunks": results,
    }, indent=2, ensure_ascii=False))
    print(f"Totals: {totals}\nSaved: {out}")
