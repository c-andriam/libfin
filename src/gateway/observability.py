"""
Correlation identifiers and structured logging.

Reconstructing one payment's path across the API, the worker and the chain used
to be manual archaeology: the log lines carried no shared identifier, so the
only way to link them was by timestamp and guesswork. During an incident that is
the difference between ten minutes and half a day — and this system's incidents
involve someone's money sitting in the wrong place.

Every log record now carries a correlation id. It enters on the request (from
the caller's ``X-Correlation-Id`` when supplied, so a merchant can trace their
own traffic), rides along to the Celery task, and appears on every line either
one emits.
"""

import contextvars
import json
import logging
import secrets
import sys
from datetime import datetime, timezone
from typing import Optional

#: The identifier for whatever this process is currently working on: one HTTP
#: request, or one Celery task. A ContextVar rather than a thread-local so it
#: survives the async hops the request path makes.
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)
#: The transaction under work, when there is one. Cheap to set, and it turns a
#: log search for "what happened to payment 4821" into one query.
transaction_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "transaction_id", default=None
)

#: Keys already present on a LogRecord; anything else an author passes via
#: `extra=` is merged into the JSON output as a field of its own.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName""".split()
)


def new_correlation_id() -> str:
    return secrets.token_hex(8)


class CorrelationFilter(logging.Filter):
    """Stamp every record with the current correlation and transaction ids."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get()
        record.transaction_id = transaction_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Structured because these logs are meant to be queried, not read: "every
    CRITICAL mentioning MANUAL ACTION in the last hour" has to be a filter, not
    a grep someone remembers to run.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }

        tx = getattr(record, "transaction_id", None)
        if tx is not None:
            payload["transaction_id"] = tx

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", structured: bool = True) -> None:
    """Install the formatter and filter on the root logger.

    Called once per process, from the API's lifespan and the worker's startup
    signal. Replaces existing handlers so Gunicorn's and Celery's own defaults
    do not emit a second, unstructured copy of every line.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(
        JsonFormatter()
        if structured
        else logging.Formatter(
            "%(asctime)s [%(correlation_id)s] %(levelname)s %(name)s: %(message)s"
        )
    )
    root.addHandler(handler)

    # Gunicorn, Uvicorn and Celery each attach their own handlers; let them
    # propagate to ours instead of printing an unstructured copy alongside it.
    # uvicorn.access is the one that emits the request lines under Gunicorn's
    # UvicornWorker, and it is easy to miss because gunicorn.access stays silent.
    for name in (
        "gunicorn.error",
        "gunicorn.access",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "celery",
        "celery.app.trace",
    ):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # Third-party chatter that says nothing about payments.
    for name in ("web3", "urllib3", "asyncio", "aiohttp"):
        logging.getLogger(name).setLevel("WARNING")


class correlation_scope:
    """Bind a correlation id (and optionally a transaction) for a block.

    Used by the Celery tasks, which do not have a request to inherit from:
    the id travels in the task's keyword arguments, so a payment's API log
    lines and its worker log lines share one identifier.
    """

    def __init__(self, cid: Optional[str] = None, tx_id: Optional[int] = None):
        self._cid = cid or new_correlation_id()
        self._tx_id = tx_id
        self._tokens = []

    def __enter__(self) -> str:
        self._tokens.append((correlation_id, correlation_id.set(self._cid)))
        if self._tx_id is not None:
            self._tokens.append((transaction_id, transaction_id.set(self._tx_id)))
        return self._cid

    def __exit__(self, *exc_info) -> None:
        for var, token in reversed(self._tokens):
            var.reset(token)
        self._tokens.clear()
