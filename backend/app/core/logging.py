"""DocuFlow - Structured JSON Logging"""
import logging
import json
import sys
import traceback
from datetime import datetime, timezone
from app.core.config import settings


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_data["exception"] = traceback.format_exception(*record.exc_info)
        for key in ("trace_id", "document_id", "client_id", "request_id"):
            if hasattr(record, key):
                log_data[key] = getattr(record, key)
        return json.dumps(log_data, ensure_ascii=False)


def setup_logging():
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    if settings.LOG_FORMAT == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers.clear()
    root.addHandler(handler)
    # Silence noisy libs
    for lib in ("uvicorn.access", "sqlalchemy.engine", "aio_pika"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
