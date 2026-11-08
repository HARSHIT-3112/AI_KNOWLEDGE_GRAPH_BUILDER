"""M10 — Structured logs with a trace id (guide §12.6).

Every log line is ONE JSON object, so tools (or `jq`) can filter it:
  {"ts": "...", "level": "INFO", "logger": "kg.worker", "msg": "stage_done",
   "trace_id": "doc-af4b…", "stage": "extracting", "seconds": 64.2}

trace_id follows one unit of work end to end:
  * API request  -> X-Request-ID header (or a fresh id), echoed back in the response
  * worker job   -> the document id, from claim to completion
So "what happened to doc-af4b…?" is one grep:  grep doc-af4b worker.log | jq .

Never log secrets or whole document text: log ids, counts, durations.
"""
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")

_STD = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id_var.get(),
        }
        out.update({k: v for k, v in vars(record).items() if k not in _STD})   # extra={...} fields
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("neo4j", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
