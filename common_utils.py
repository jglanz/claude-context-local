"""Common utilities shared across modules."""

import contextvars
import logging
import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_storage_dir() -> Path:
    """Get or create base storage directory. Cached for performance."""
    storage_path = os.getenv('CODE_SEARCH_STORAGE', str(Path.home() / '.claude_code_search'))
    storage_dir = Path(storage_path)
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir


# ---------------------------------------------------------------------------
# Per-record project tag for logging.
#
# The MCP server is project-agnostic at startup — the "current project" is only
# known once an index/search call comes in. We carry that name through a
# ContextVar and inject it onto every LogRecord via ProjectLogFilter so logs
# from any module (chunker, embedder, FAISS, MCP) are attributable.
# ---------------------------------------------------------------------------

_current_project: contextvars.ContextVar[str] = contextvars.ContextVar(
    "code_search_current_project", default="-"
)


def get_current_project() -> str:
    return _current_project.get()


@contextmanager
def project_context(name: str):
    """Set the current-project tag for the duration of a code block.

    Safe across threads/coroutines because ContextVar is per-context.
    """
    token = _current_project.set(name or "-")
    try:
        yield
    finally:
        _current_project.reset(token)


class ProjectLogFilter(logging.Filter):
    """Logging filter that adds `record.project` so format strings can use it."""

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        record.project = _current_project.get()
        return True

