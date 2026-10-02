"""structured logging. json in containers, readable console output locally.

every log line carries whatever is bound in the contextvars (request_id, project_id,
migration_run_id, stage...). bind with `structlog.contextvars.bind_contextvars`, or wrap a
pipeline step in `stage()`, which binds the step's context, times it and says how it ended.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TextIO

import structlog

from app.core.config import get_settings

_configured = False


def configure_logging(force: bool = False, *, stream: TextIO | None = None) -> None:
    global _configured
    if _configured and not force:
        return

    settings = get_settings()
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    # the renderer lives on the handler, not in the logger chain, so a logger cached before a
    # reconfigure still renders the new way. json gets the traceback as a string field.
    renderers: list = (
        [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
        if settings.log_format == "json"
        else [structlog.dev.ConsoleRenderer()]
    )

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderers],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn ships its own handlers; route them through ours so every line has one shape
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # the request middleware already logs one line per request with its duration and id
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def error_type_of(exc: BaseException) -> str:
    """the vocabulary the api answers with when the exception has one, the class name if not."""
    return str(getattr(exc, "error_type", None) or exc.__class__.__name__)


@dataclass
class StageLog:
    """what a stage reports when it finishes. set `record_count` and add fields as the step
    learns them; they go on the `stage_finished` line."""

    record_count: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def note(self, **fields: Any) -> None:
        self.fields.update(fields)


_stage_log = get_logger("app.stage")


@contextmanager
def stage(name: str, **context: Any) -> Iterator[StageLog]:
    """one pipeline step: binds `stage` and the given context (project_id, migration_run_id,
    dataset, entity...) for every line logged inside it, then logs `stage_finished` with
    duration_ms and record_count, or `stage_failed` with error_type and the traceback.

    Stages nest. The outer stage's name comes back when an inner one ends, and context bound
    by the outer one stays on the inner one's lines, so filtering on a run id finds them all.
    """
    tokens = structlog.contextvars.bind_contextvars(stage=name, **context)
    started = time.perf_counter()
    report = StageLog()
    _stage_log.info("stage_started")
    try:
        yield report
    except Exception as exc:
        # an AppError is an answer the api gives on purpose (feed down, wrong state); anything
        # else is a bug and gets its traceback, once, from the innermost stage that saw it
        expected = hasattr(exc, "error_type")
        first = not getattr(exc, "_stage_logged", False)
        emit = _stage_log.warning if expected else _stage_log.error
        emit(
            "stage_failed",
            error_type=error_type_of(exc),
            error=str(exc)[:500],
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            record_count=report.record_count,
            exc_info=not expected and first,
            **report.fields,
        )
        try:
            exc._stage_logged = True  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - exceptions with __slots__
            pass
        raise
    else:
        _stage_log.info(
            "stage_finished",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            record_count=report.record_count,
            **report.fields,
        )
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
