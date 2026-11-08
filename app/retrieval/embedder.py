"""M7 — Text -> vector (guide §9.1).

An embedding model maps text to a point in a 1024-dimensional space where
texts with similar MEANING land close together, even with different words:
  "duplicate nodes after re-running"  ~  "idempotent ingestion"

Two details that matter in practice:
  * Asymmetric models: mxbai-embed-large wants questions prefixed with an
    instruction, while passages are embedded as-is. Mixing that up quietly
    lowers retrieval quality.
  * The dimension is a contract with the vector index. We check it on every
    call so a model swap fails loudly instead of corrupting the index.
"""
import httpx

from app.core.config import get_settings

# Per-model query instructions (passages get no prefix).
QUERY_PREFIXES = {
    "mxbai-embed-large": "Represent this sentence for searching relevant passages: ",
}


class OllamaEmbedder:
    def __init__(self, model: str | None = None, dimensions: int | None = None, url: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        s = get_settings()
        self.model = model or s.embedding_model
        self.dimensions = dimensions or s.embedding_dimensions
        self._http = httpx.Client(base_url=url or s.ollama_url, timeout=120, transport=transport)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = self._http.post("/api/embed", json={"model": self.model, "input": texts}).raise_for_status().json()
        vectors = body["embeddings"]
        if len(vectors) != len(texts):
            raise RuntimeError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        for v in vectors:
            if len(v) != self.dimensions:
                raise RuntimeError(f"{self.model} returned {len(v)} dims, index expects {self.dimensions}")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, question: str) -> list[float]:
        return self._embed([QUERY_PREFIXES.get(self.model.split(":")[0], "") + question])[0]

    def close(self) -> None:
        self._http.close()
