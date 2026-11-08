"""M3: file -> validated -> page text -> stable chunks -> Neo4j (Document + Chunk nodes).

Acceptance test: you can trace one source paragraph to a stable chunk id,
and running this twice on the same file does not duplicate anything.

Run:  .venv/bin/python -m scripts.m3_ingest_document documents/sample_company.txt
      .venv/bin/python -m scripts.m3_ingest_document path/to/any.pdf --max-chars 800
"""
import argparse
from dataclasses import asdict

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.graph.repository import count_graph, create_schema, upsert_document_with_chunks
from app.ingestion.chunker import chunk_pages
from app.ingestion.file_validator import validate_file
from app.ingestion.parsers import parse_file

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--max-chars", type=int, default=600)
    ap.add_argument("--overlap", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true", help="parse + chunk only, don't write to Neo4j")
    args = ap.parse_args()

    info = validate_file(args.path)
    pages = parse_file(info.path)
    status = "chunked" if pages else "extraction_failed"     # guide §2.2: no text -> don't go further
    chunks = chunk_pages(info.id, pages, args.max_chars, args.overlap)

    print(f"Document {info.id}  ({info.filename}, {info.size_bytes} bytes, {len(pages)} page(s), status={status})")
    for c in chunks:
        preview = c.text[:90].replace("\n", " ")
        print(f"  [{c.index}] {c.id}  page {c.page}  {len(c.text):>4} chars  | {preview}...")

    if args.dry_run:
        raise SystemExit(0)

    db = get_settings().neo4j_database
    driver = get_driver()
    try:
        create_schema(driver, db)
        doc = {"id": info.id, "filename": info.filename, "sha256": info.sha256,
               "size_bytes": info.size_bytes, "status": status}
        with driver.session(database=db) as session:
            session.execute_write(upsert_document_with_chunks, doc, [asdict(c) for c in chunks])
        print("\nGraph now:", count_graph(driver, db))
        print("Try in Neo4j Browser:\n"
              f"  MATCH p=(:Document {{id: '{info.id}'}})-[:HAS_CHUNK]->(:Chunk) RETURN p")
    finally:
        close_driver()
