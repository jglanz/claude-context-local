"""Top-level chunking worker for ProcessPoolExecutor."""

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from chunking.code_chunk import CodeChunk
from chunking.multi_language_chunker import MultiLanguageChunker

logger = logging.getLogger(__name__)

# Each worker process keeps a single MultiLanguageChunker alive so tree-sitter
# language objects load once per worker rather than once per file.
_CHUNKER: Optional[MultiLanguageChunker] = None
_CHUNKER_ROOT: Optional[str] = None


def _get_chunker(root: str) -> MultiLanguageChunker:
    global _CHUNKER, _CHUNKER_ROOT
    if _CHUNKER is None or _CHUNKER_ROOT != root:
        _CHUNKER = MultiLanguageChunker(root)
        _CHUNKER_ROOT = root
    return _CHUNKER


def chunk_one(task: Tuple[str, str]) -> List[CodeChunk]:
    """Chunk a single file. Returns [] on any failure (logged in worker)."""
    project_path, rel_file = task
    full = str(Path(project_path) / rel_file)
    try:
        return _get_chunker(project_path).chunk_file(full)
    except Exception as e:
        logger.warning(f"Chunk worker failed on {rel_file}: {e}")
        return []
