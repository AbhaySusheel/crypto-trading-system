import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

LOG_CATEGORIES = {
    "execution": "logs/execution.log",
    "reconciliation": "logs/reconciliation.log",
    "persistence": "logs/errors.log",
    "websocket": "logs/execution.log",
    "risk": "logs/execution.log",
    "fail_safe": "logs/errors.log",
}

DEFAULT_LOG_LEVEL = logging.INFO
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
ERROR_LOG_PATH = "logs/errors.log"


def _ensure_error_handler(logger: logging.Logger) -> None:
    logs_dir = get_logs_dir()
    file_path = os.path.join(logs_dir, os.path.basename(ERROR_LOG_PATH))
    if any(
        isinstance(handler, RotatingFileHandler)
        and getattr(handler, "baseFilename", None)
        and os.path.basename(handler.baseFilename) == os.path.basename(ERROR_LOG_PATH)
        for handler in logger.handlers
    ):
        return

    try:
        handler = RotatingFileHandler(
            file_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(logging.WARNING)
        handler.setFormatter(StructuredLogFormatter())
        logger.addHandler(handler)
    except Exception:
        pass

class StructuredLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "severity": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
            "symbol": getattr(record, "symbol", "-"),
            "trade_id": getattr(record, "trade_id", "-"),
            "order_id": getattr(record, "order_id", "-"),
            "metadata": self._normalize_metadata(getattr(record, "metadata", {})),
        }
        if hasattr(record, "category"):
            payload["category"] = record.category
        return json.dumps(payload, default=str)

    def _normalize_metadata(self, metadata: Any) -> Any:
        if metadata is None:
            return {}
        if isinstance(metadata, dict):
            return metadata
        try:
            return json.loads(json.dumps(metadata, default=str))
        except Exception:
            return {"raw": str(metadata)}


class StructuredLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        extra = kwargs.get("extra", {}) or {}
        merged = {**self.extra, **extra}
        kwargs["extra"] = merged
        return msg, kwargs


def get_logs_dir() -> str:
    root = Path(__file__).resolve().parents[1]
    logs_dir = root / "logs"
    os.makedirs(logs_dir, exist_ok=True)
    return str(logs_dir)


def _ensure_logger(category: str) -> logging.Logger:
    logger = logging.getLogger(category)
    logger.setLevel(DEFAULT_LOG_LEVEL)
    logger.propagate = False

    if not logger.handlers:
        _ensure_error_handler(logger)
        logs_dir = get_logs_dir()
        filename = LOG_CATEGORIES.get(category, ERROR_LOG_PATH)
        file_path = os.path.join(logs_dir, os.path.basename(filename))

        try:
            handler = RotatingFileHandler(
                file_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setLevel(DEFAULT_LOG_LEVEL)
            handler.setFormatter(StructuredLogFormatter())
            logger.addHandler(handler)
        except Exception:
            # Fail-open: logging configuration errors must not interrupt trading.
            pass

    return logger


def get_logger(category: str, default_metadata: Optional[Dict[str, Any]] = None) -> StructuredLoggerAdapter:
    if category not in LOG_CATEGORIES:
        category = "execution"
    logger = _ensure_logger(category)
    metadata = default_metadata or {}
    return StructuredLoggerAdapter(logger, {
        "category": category,
        "symbol": "-",
        "trade_id": "-",
        "order_id": "-",
        "metadata": metadata,
    })


def log_event(
    category: str,
    level: int,
    message: str,
    symbol: Optional[str] = None,
    trade_id: Optional[str] = None,
    order_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    logger = get_logger(category)
    extra = {
        "symbol": symbol or "-",
        "trade_id": trade_id or "-",
        "order_id": str(order_id) if order_id is not None else "-",
        "metadata": metadata or {},
    }
    try:
        logger.log(level, message, extra=extra)
    except Exception:
        # Fail-open: do not interrupt trading on logging issues.
        pass
