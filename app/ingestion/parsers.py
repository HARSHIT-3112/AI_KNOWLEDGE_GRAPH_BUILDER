"""M3 — Turn a file into page-aware text (guide §6.1, §6.4).

Output is always the same shape: [{"page": int, "text": str}, ...]
so the chunker doesn't care whether the source was a PDF or a TXT.
Keeping the page number is the start of PROVENANCE: later every fact
can say "document X, page 3".
"""
import re
from pathlib import Path

import pymupdf  # PyMuPDF


def _clean(text: str) -> str:
    # Normalise whitespace but KEEP paragraph breaks (blank lines).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pdf(path: Path) -> list[dict]:
    pages = []
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, start=1):
            text = _clean(page.get_text("text") or "")
            if text:                       # scanned pages come back empty -> would need OCR
                pages.append({"page": number, "text": text})
    return pages


def parse_text(path: Path) -> list[dict]:
    text = _clean(path.read_text(encoding="utf-8", errors="replace"))
    return [{"page": 1, "text": text}] if text else []


def parse_file(path: Path) -> list[dict]:
    return parse_pdf(path) if path.suffix.lower() == ".pdf" else parse_text(path)
