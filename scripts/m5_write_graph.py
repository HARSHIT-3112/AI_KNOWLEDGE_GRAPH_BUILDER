"""M5/M6: saved extraction (from M4) -> re-validate -> resolve entities -> Neo4j with provenance.

No LLM call here: we load the raw model output saved by m4_extract and run it
through the CURRENT validator. Improve the validator -> rerun this script ->
the graph updates, without paying for extraction again.

Acceptance test (guide M5): run it twice — the counts must not change.

Run:
  .venv/bin/python -m scripts.m5_write_graph documents/sample_company.txt
  .venv/bin/python -m scripts.m5_write_graph documents/sample_company.txt --delete   # remove doc + its facts
"""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.extraction.validator import validate_extraction
from app.graph.graph_writer import delete_document, write_document_graph
from app.graph.repository import count_graph, create_schema
from app.ingestion.chunker import chunk_pages
from app.ingestion.file_validator import validate_file
from app.ingestion.parsers import parse_file
from app.schemas.extraction import Extraction


def latest_extraction(doc_id: str) -> Path:
    files = sorted(Path("extractions").glob(f"{doc_id}.*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"No extraction for {doc_id}. Run: python -m scripts.m4_extract <file>")
    return files[-1]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--extraction", help="specific extractions/*.json (default: newest for this file)")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    info = validate_file(args.path)
    db = get_settings().neo4j_database
    driver = get_driver()
    try:
        create_schema(driver, db)
        before = count_graph(driver, db)
        if args.delete:
            with driver.session(database=db) as s:
                print("Deleted:", s.execute_write(delete_document, info.id))
            print(f"graph before {before}\ngraph after  {count_graph(driver, db)}")
            raise SystemExit(0)

        ext_path = Path(args.extraction) if args.extraction else latest_extraction(info.id)
        saved = json.loads(ext_path.read_text())
        chunks = chunk_pages(info.id, parse_file(info.path), saved.get("max_chars", 600), saved.get("overlap", 100))
        texts = {c.id: c.text for c in chunks}

        reports = []
        for ch in saved["chunks"]:
            if ch.get("status") != "ok":
                continue
            if ch["chunk_id"] not in texts:      # chunk settings changed since extraction
                raise SystemExit(f"Chunk {ch['chunk_id']} not found: re-run m4_extract with the same chunk settings")
            reports.append(validate_extraction(Extraction.model_validate_json(ch["raw"]),
                                               texts[ch["chunk_id"]], ch["chunk_id"]))

        doc = {"id": info.id, "filename": info.filename, "sha256": info.sha256,
               "size_bytes": info.size_bytes, "status": "graph_written"}
        with driver.session(database=db) as s:
            stats = s.execute_write(write_document_graph, doc, [asdict(c) for c in chunks], reports,
                                    saved["model"], saved["prompt_version"])
        print(f"Extraction file: {ext_path}")
        print("Write stats:", asdict(stats))
        print(f"graph before {before}\ngraph after  {count_graph(driver, db)}")
        print("\nExplore in Neo4j Browser — see lessons/02_provenance_queries.cypher")
    finally:
        close_driver()
