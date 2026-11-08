"""M3 — Paragraph-aware chunking with stable IDs (guide §6.3, §6.5).

Improvements over the guide's teaching version (it mentions these as TODOs):
  * A single huge paragraph is split on sentence boundaries, so no chunk
    exceeds max_chars (unless one sentence alone is longer — then hard split).
  * Overlap starts at a word boundary, not mid-word.
  * Every chunk keeps its page number and gets a deterministic id:
        hash(document_id + chunk_index + text)
    -> same document, same settings => exactly the same chunk ids.
"""
import hashlib
import re
from dataclasses import dataclass

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    index: int
    page: int
    text: str


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Break one oversized paragraph into sentence-packed pieces <= max_chars."""
    pieces, current = [], ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > max_chars:                 # pathological: no sentence breaks
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                pieces.append(current)
            current = sentence
    if current:
        pieces.append(current)
    return pieces


def _tail(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return text if overlap_chars > 0 else ""
    cut = text[-overlap_chars:]
    space = cut.find(" ")
    return cut[space + 1:] if space != -1 else cut


def chunk_text(text: str, max_chars: int = 1500, overlap_chars: int = 200) -> list[str]:
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be smaller than max_chars")
    paragraphs = []
    for p in (p.strip() for p in text.split("\n\n")):
        if p:
            paragraphs.extend(_split_long(p, max_chars) if len(p) > max_chars else [p])

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        tail = _tail(current, overlap_chars)
        current = f"{tail}\n\n{paragraph}".strip() if len(tail) + len(paragraph) + 2 <= max_chars else paragraph
    if current:
        chunks.append(current)
    return chunks


def chunk_id(document_id: str, index: int, text: str) -> str:
    digest = hashlib.sha256(f"{document_id}\x1f{index}\x1f{text}".encode("utf-8")).hexdigest()
    return f"chunk-{digest[:16]}"


def chunk_pages(document_id: str, pages: list[dict], max_chars: int = 1500, overlap_chars: int = 200) -> list[Chunk]:
    """Chunk page by page so every chunk maps to exactly one page."""
    out: list[Chunk] = []
    for page in pages:
        for text in chunk_text(page["text"], max_chars, overlap_chars):
            index = len(out)
            out.append(Chunk(chunk_id(document_id, index, text), document_id, index, page["page"], text))
    return out
