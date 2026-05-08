"""Tree-sitter based code chunking with support for multiple languages."""

import logging
from pathlib import Path
from typing import List, Optional

from chunking.base_chunker import TreeSitterChunk, AVAILABLE_LANGUAGES
from chunking.languages import LANGUAGE_MAP

logger = logging.getLogger(__name__)

class TreeSitterChunker:
    """Main tree-sitter chunker that delegates to language-specific implementations."""

    def __init__(self):
        """Initialize the tree-sitter chunker."""
        self.chunkers = {}

    def get_chunker(self, file_path: str):
        """Get the appropriate chunker for a file.

        Args:
            file_path: Path to the file

        Returns:
            LanguageChunker instance or None if unsupported
        """
        suffix = Path(file_path).suffix.lower()

        if suffix not in LANGUAGE_MAP:
            return None

        language_name, chunker_class = LANGUAGE_MAP[suffix]

        # Check if language is available
        if language_name not in AVAILABLE_LANGUAGES:
            logger.debug(f"Language {language_name} not available. Install tree-sitter-{language_name}")
            return None

        # Lazy initialization of chunkers
        if suffix not in self.chunkers:
            assert callable(chunker_class), f"Chunker should be callable, got {type(chunker_class)}"
            self.chunkers[suffix] = chunker_class()

        return self.chunkers[suffix]

    def chunk_file(self, file_path: str, content: Optional[str] = None) -> List[TreeSitterChunk]:
        """Chunk a file into semantic units.

        Args:
            file_path: Path to the file
            content: Optional file content (will read from file if not provided)

        Returns:
            List of TreeSitterChunk objects
        """
        chunker = self.get_chunker(file_path)

        if not chunker:
            logger.debug(f"No tree-sitter chunker available for {file_path}")
            return []

        if content is None:
            # Cheap pre-check: bail on binary or non-utf8 sources before doing
            # the full read + tree-sitter parse. Costs ~4 KB of I/O per file.
            try:
                with open(file_path, 'rb') as fb:
                    head = fb.read(4096)
            except OSError as e:
                logger.error(f"Failed to read file {file_path}: {e}")
                return []
            if b"\x00" in head:
                logger.debug(f"Skipping binary file: {file_path}")
                return []
            try:
                head.decode("utf-8")
            except UnicodeDecodeError:
                logger.debug(f"Skipping non-utf8 file: {file_path}")
                return []
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                logger.error(f"Failed to read file {file_path}: {e}")
                return []

        try:
            return chunker.chunk_code(content)
        except Exception as e:
            logger.warning(f"Tree-sitter parsing failed for {file_path}: {e}")
            return []

    def is_supported(self, file_path: str) -> bool:
        """Check if a file type is supported.

        Args:
            file_path: Path to the file

        Returns:
            True if file type is supported
        """
        suffix = Path(file_path).suffix.lower()
        if suffix not in LANGUAGE_MAP:
            return False

        language_name, _ = LANGUAGE_MAP[suffix]
        return language_name in AVAILABLE_LANGUAGES
