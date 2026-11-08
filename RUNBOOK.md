# Runbook — AI Knowledge Graph Builder

How to run, check, and fix the system. Written for "it's 11 pm and something is wrong".

## Architecture in one picture

```
            ┌──────────── Docker (docker-compose.yml) ────────────┐
 client ──► │  api :8000  ──enqueue──►  neo4j :7687  ◄──claim──  worker │
            │  (FastAPI)              (graph + job queue)  (pipeline)  │
            └────────────────────────────────────────────────┬────────┘
                                                             ▼
                                           Ollama on the Mac :11434 (qwen2.5:7b, mxbai-embed-large)
```
All ports are bound to `127.0.0.1` (not reachable from other machines).

## Start / stop

```bash
open -a Docker && open -a Ollama          # both must be running
docker compose up -d --build              # start (or update after code changes)
docker compose ps                         # all three should say (healthy)
docker compose down                       # stop (data stays in the kg_neo4j_data volume)
```
API docs: http://localhost:8000/docs (Authorize with `API_KEY` from `.env`). Neo4j Browser: http://localhost:7475.

## Health checks

| Check | Meaning | Command |
|---|---|---|
| `/health/live` | API process is up (Docker restarts it if this fails) | `curl localhost:8000/health/live` |
| `/health/ready` | API can reach Neo4j **and** Ollama | `curl localhost:8000/health/ready` |
| `/metrics` | documents by status, review backlog, graph size, oldest running job | `curl -H "X-API-Key: $KEY" localhost:8000/metrics` |
| worker heartbeat | worker loop alive (15 min allowance for slow LLM calls) | `docker compose ps worker` |

`KEY=$(grep '^API_KEY=' .env | cut -d= -f2)`

## Logs

One JSON object per line. `trace_id` = request id (API) or document id (worker).
```bash
docker compose logs -f worker                                     # live
docker compose logs worker --no-log-prefix | grep doc-2ac60d93    # one document, end to end
docker compose logs api --no-log-prefix | grep '"level": "ERROR"' # errors only
```
Send `X-Request-ID: something` with a request to find it later in the API log.

## Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| `/health/ready` → `ollama: down` | Ollama app not running | `open -a Ollama`; jobs in `retry` resume automatically |
| `/health/ready` → `neo4j: down` | container stopped / still starting | `docker compose ps`, `docker compose logs neo4j` |
| documents stuck in `retry` | a dependency was down; backoff 30 s → 60 s → 120 s | fix the dependency; they continue. After 3 tries → `failed` |
| document `failed` | gave up after retries | read `error` in `GET /documents/{id}`, fix, then `POST /documents/{id}/reprocess` |
| `extraction_failed` | file has no text (scanned PDF) | needs OCR — not supported yet |
| `rejected` | not a valid PDF/TXT/MD, or file changed on disk | re-upload a valid file |
| `oldest_processing_since` is old | worker crashed mid-job | it's requeued after 30 min automatically; or `docker compose restart worker` |
| process killed / exit 137 | out of memory (8 GB Mac) | quit other apps; `ollama stop qwen2.5:7b` when idle; limits are in `docker-compose.yml` |
| `429 Too Many Requests` | rate limit (ask 10/min, upload 30/min) | wait `Retry-After` seconds; tune `RATE_*` in `.env` |
| `review_pending` keeps growing | extractor producing doubtful facts | look at `GET /review`; adjust prompt/validator, re-run extraction |

## Backup & restore

```bash
bash scripts/backup.sh     # ~1 min, ~30 s of downtime
```
Creates `backups/<timestamp>/neo4j.dump` **and proves it restores**: it loads the dump into a throwaway
database and compares node/relationship counts with the live ones. "RESTORE TEST PASSED" or it fails loudly.
Uploaded files (`documents/uploads/`) and raw LLM outputs (`extractions/`) are plain folders — copy them too.

**Restoring for real** (replaces the live database — be sure):
```bash
docker compose stop -t 120 worker api neo4j
docker run --rm -v ai_knowledge_graph_kg_neo4j_data:/data -v "$PWD/backups/<timestamp>:/backups" \
  neo4j:2026.10.1 neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true
docker compose start neo4j api worker
```

## Before changing code

```bash
bash scripts/check.sh      # all tests + retrieval regression gate (hybrid Recall@5 ≥ 0.93, MRR ≥ 0.80)
docker compose up -d --build
```

## Rotating secrets

- **API key:** change `API_KEY` in `.env` → `docker compose up -d` (api restarts) → update clients.
- **Neo4j password:** `ALTER CURRENT USER SET PASSWORD FROM 'old' TO 'new'` in the Browser →
  update `NEO4J_PASSWORD` in `.env` → `docker compose up -d`.

## Known limits (honest list)

- One worker, polling Neo4j. Fine for a laptop; for many documents use Redis + RQ/Celery and several workers.
- Extraction is ~70 % of processing time (≈80 s per chunk on an 8 GB M2). A GPU server or hosted LLM is the real scaling lever.
- Rate limits are in-process memory — multiple API replicas would each have their own.
- No user accounts/tenants: one API key = full access. Per-document permissions are needed before multi-user use.
- No OCR for scanned PDFs. No alerting — `/metrics` exists but nothing watches it yet.
