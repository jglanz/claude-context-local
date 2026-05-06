"""Incremental indexing using Merkle tree change detection."""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from merkle.change_detector import ChangeDetector, FileChanges
from merkle.merkle_dag import MerkleDAG
from merkle.snapshot_manager import SnapshotManager
from chunking.multi_language_chunker import MultiLanguageChunker
from embeddings.embedder import CodeEmbedder
from search.indexer import CodeIndexManager as Indexer

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid int for {name}={raw!r}; using default {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Invalid float for {name}={raw!r}; using default {default}")
        return default


# Default flush thresholds; env vars override at instantiation time.
DEFAULT_FLUSH_FILES = 200
DEFAULT_FLUSH_BYTES = 256 * 1024 * 1024  # 256 MB of chunked content
DEFAULT_FLUSH_SECONDS = 60.0


@dataclass
class IncrementalIndexResult:
    """Result of incremental indexing operation."""
    
    files_added: int
    files_removed: int
    files_modified: int
    chunks_added: int
    chunks_removed: int
    time_taken: float
    success: bool
    error: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'files_added': self.files_added,
            'files_removed': self.files_removed,
            'files_modified': self.files_modified,
            'chunks_added': self.chunks_added,
            'chunks_removed': self.chunks_removed,
            'time_taken': self.time_taken,
            'success': self.success,
            'error': self.error
        }


class IncrementalIndexer:
    """Handles incremental indexing of code changes."""
    
    def __init__(
        self,
        indexer: Optional[Indexer] = None,
        embedder: Optional[CodeEmbedder] = None,
        chunker: Optional[MultiLanguageChunker] = None,
        snapshot_manager: Optional[SnapshotManager] = None,
        flush_every_files: Optional[int] = None,
        flush_every_bytes: Optional[int] = None,
        flush_every_seconds: Optional[float] = None,
    ):
        """Initialize incremental indexer.

        Args:
            indexer: Indexer instance
            embedder: Embedder instance
            chunker: Code chunker instance
            snapshot_manager: Snapshot manager instance
            flush_every_files: Flush index after this many files (env: CODE_SEARCH_FLUSH_FILES)
            flush_every_bytes: Flush index after this many bytes of chunked content
                (env: CODE_SEARCH_FLUSH_BYTES)
            flush_every_seconds: Flush index after this many seconds since the last flush
                (env: CODE_SEARCH_FLUSH_SECONDS)
        """
        self.indexer = indexer or Indexer()
        self.embedder = embedder or CodeEmbedder()
        self.chunker = chunker or MultiLanguageChunker()
        self.snapshot_manager = snapshot_manager or SnapshotManager()
        self.change_detector = ChangeDetector(self.snapshot_manager)

        self.flush_every_files = (
            flush_every_files
            if flush_every_files is not None
            else _env_int("CODE_SEARCH_FLUSH_FILES", DEFAULT_FLUSH_FILES)
        )
        self.flush_every_bytes = (
            flush_every_bytes
            if flush_every_bytes is not None
            else _env_int("CODE_SEARCH_FLUSH_BYTES", DEFAULT_FLUSH_BYTES)
        )
        self.flush_every_seconds = (
            flush_every_seconds
            if flush_every_seconds is not None
            else _env_float("CODE_SEARCH_FLUSH_SECONDS", DEFAULT_FLUSH_SECONDS)
        )
        logger.info(
            "Indexer flush thresholds: "
            f"files={self.flush_every_files}, "
            f"bytes={self.flush_every_bytes}, "
            f"seconds={self.flush_every_seconds}"
        )
    
    def detect_changes(self, project_path: str) -> Tuple[FileChanges, MerkleDAG]:
        """Detect changes in project since last snapshot.
        
        Args:
            project_path: Path to project
            
        Returns:
            Tuple of (FileChanges, current MerkleDAG)
        """
        return self.change_detector.detect_changes_from_snapshot(project_path)
    
    def incremental_index(
        self,
        project_path: str,
        project_name: Optional[str] = None,
        force_full: bool = False
    ) -> IncrementalIndexResult:
        """Perform incremental indexing of a project.
        
        Args:
            project_path: Path to project
            project_name: Optional project name
            force_full: Force full reindex even if snapshot exists
            
        Returns:
            IncrementalIndexResult with statistics
        """
        start_time = time.time()
        project_path = str(Path(project_path).resolve())
        
        if not project_name:
            project_name = Path(project_path).name
        
        try:
            # Check if we should do full index
            if force_full or not self.snapshot_manager.has_snapshot(project_path):
                logger.info(f"Performing full index for {project_name}")
                return self._full_index(project_path, project_name, start_time)
            
            # Detect changes
            logger.info(f"Detecting changes in {project_name}")
            changes, current_dag = self.detect_changes(project_path)
            
            if not changes.has_changes():
                logger.info(f"No changes detected in {project_name}")
                return IncrementalIndexResult(
                    files_added=0,
                    files_removed=0,
                    files_modified=0,
                    chunks_added=0,
                    chunks_removed=0,
                    time_taken=time.time() - start_time,
                    success=True
                )
            
            # Log changes
            logger.info(
                f"Changes detected - Added: {len(changes.added)}, "
                f"Removed: {len(changes.removed)}, Modified: {len(changes.modified)}"
            )
            
            # Process changes
            chunks_removed = self._remove_old_chunks(changes, project_name)
            chunks_added = self._add_new_chunks(changes, project_path, project_name)
            
            # Update snapshot
            self.snapshot_manager.save_snapshot(current_dag, {
                'project_name': project_name,
                'incremental_update': True,
                'files_added': len(changes.added),
                'files_removed': len(changes.removed),
                'files_modified': len(changes.modified)
            })
            
            # Update index
            self.indexer.save_index()
            
            return IncrementalIndexResult(
                files_added=len(changes.added),
                files_removed=len(changes.removed),
                files_modified=len(changes.modified),
                chunks_added=chunks_added,
                chunks_removed=chunks_removed,
                time_taken=time.time() - start_time,
                success=True
            )
            
        except Exception as e:
            logger.error(f"Incremental indexing failed: {e}")
            return IncrementalIndexResult(
                files_added=0,
                files_removed=0,
                files_modified=0,
                chunks_added=0,
                chunks_removed=0,
                time_taken=time.time() - start_time,
                success=False,
                error=str(e)
            )
    
    def _full_index(
        self,
        project_path: str,
        project_name: str,
        start_time: float
    ) -> IncrementalIndexResult:
        """Perform full indexing of a project.
        
        Args:
            project_path: Path to project
            project_name: Project name
            start_time: Start time for timing
            
        Returns:
            IncrementalIndexResult
        """
        try:
            # Clear existing index
            self.indexer.clear_index()

            # Build DAG for all files
            dag = MerkleDAG(project_path)
            dag.build()
            all_files = dag.get_all_files()

            # Filter supported files
            supported_files = [f for f in all_files if self.chunker.is_supported(f)]

            chunks_added = self._chunk_embed_persist_batched(
                supported_files, project_path, project_name
            )

            # Save snapshot only after all batches succeeded
            self.snapshot_manager.save_snapshot(dag, {
                'project_name': project_name,
                'full_index': True,
                'total_files': len(all_files),
                'supported_files': len(supported_files),
                'chunks_indexed': chunks_added
            })

            # Final save (most recent batch was already flushed, but this is cheap)
            self.indexer.save_index()
            
            return IncrementalIndexResult(
                files_added=len(supported_files),
                files_removed=0,
                files_modified=0,
                chunks_added=chunks_added,
                chunks_removed=0,
                time_taken=time.time() - start_time,
                success=True
            )
            
        except Exception as e:
            logger.error(f"Full indexing failed: {e}")
            return IncrementalIndexResult(
                files_added=0,
                files_removed=0,
                files_modified=0,
                chunks_added=0,
                chunks_removed=0,
                time_taken=time.time() - start_time,
                success=False,
                error=str(e)
            )
    
    def _remove_old_chunks(self, changes: FileChanges, project_name: str) -> int:
        """Remove chunks for deleted and modified files.
        
        Args:
            changes: File changes
            project_name: Project name
            
        Returns:
            Number of chunks removed
        """
        files_to_remove = self.change_detector.get_files_to_remove(changes)
        chunks_removed = 0
        
        for file_path in files_to_remove:
            # Remove from metadata
            removed = self.indexer.remove_file_chunks(file_path, project_name)
            chunks_removed += removed
            logger.debug(f"Removed {removed} chunks from {file_path}")
        
        return chunks_removed
    
    def _add_new_chunks(
        self,
        changes: FileChanges,
        project_path: str,
        project_name: str
    ) -> int:
        """Add chunks for new and modified files.
        
        Args:
            changes: File changes
            project_path: Project root path
            project_name: Project name
            
        Returns:
            Number of chunks added
        """
        files_to_index = self.change_detector.get_files_to_reindex(changes)

        # Filter supported files
        supported_files = [f for f in files_to_index if self.chunker.is_supported(f)]

        return self._chunk_embed_persist_batched(
            supported_files, project_path, project_name
        )

    def _chunk_embed_persist_batched(
        self,
        supported_files: List[str],
        project_path: str,
        project_name: str,
    ) -> int:
        """Chunk, embed, and persist files in batches with periodic flushing.

        Buffers chunks in memory until any of the configured thresholds
        (file count, byte count, elapsed time) is exceeded, then embeds and
        writes the index to disk. On exception, flushes whatever is buffered
        before re-raising so partial progress isn't lost.

        Returns:
            Total number of chunks embedded and added to the index.
        """
        chunks_buffer: list = []
        bytes_buffer = 0
        files_in_batch = 0
        total_chunks = 0
        files_processed = 0
        last_flush_time = time.time()
        start = time.time()

        def flush(reason: str) -> int:
            nonlocal chunks_buffer, bytes_buffer, files_in_batch, last_flush_time
            if not chunks_buffer:
                return 0
            buf = chunks_buffer
            batch_files = files_in_batch
            batch_bytes = bytes_buffer
            chunks_buffer = []
            bytes_buffer = 0
            files_in_batch = 0
            flushed = self._embed_and_persist_batch(buf, project_name)
            elapsed = time.time() - last_flush_time
            last_flush_time = time.time()
            logger.info(
                f"Flushed batch ({reason}): {flushed} chunks from "
                f"{batch_files} files / {batch_bytes / 1024 / 1024:.1f} MB "
                f"in {elapsed:.1f}s"
            )
            return flushed

        try:
            for file_path in supported_files:
                full_path = Path(project_path) / file_path
                try:
                    chunks = self.chunker.chunk_file(str(full_path))
                except Exception as e:
                    logger.warning(f"Failed to chunk {file_path}: {e}")
                    chunks = []

                files_processed += 1
                files_in_batch += 1
                if chunks:
                    chunks_buffer.extend(chunks)
                    bytes_buffer += sum(
                        len(c.content.encode("utf-8", errors="ignore")) for c in chunks
                    )

                if files_in_batch >= self.flush_every_files:
                    total_chunks += flush("file count")
                elif bytes_buffer >= self.flush_every_bytes:
                    total_chunks += flush("byte count")
                elif (time.time() - last_flush_time) >= self.flush_every_seconds:
                    total_chunks += flush("elapsed time")

            total_chunks += flush("final")
            logger.info(
                f"Indexing complete: {total_chunks} chunks from "
                f"{files_processed} files in {time.time() - start:.1f}s"
            )
            return total_chunks
        except Exception:
            try:
                total_chunks += flush("error recovery")
                logger.error(
                    f"Indexing aborted after {files_processed} files; "
                    f"flushed {total_chunks} chunks before failure"
                )
            except Exception as inner:
                logger.error(f"Failed to flush during error recovery: {inner}")
            raise

    def _embed_and_persist_batch(self, chunks: list, project_name: str) -> int:
        """Embed a buffered batch of chunks, add to index, and persist to disk."""
        if not chunks:
            return 0
        try:
            embedding_results = self.embedder.embed_chunks(chunks)
        except Exception as e:
            logger.warning(f"Embedding failed for batch of {len(chunks)} chunks: {e}")
            return 0
        for chunk, result in zip(chunks, embedding_results):
            result.metadata['project_name'] = project_name
            result.metadata['content'] = chunk.content
        if embedding_results:
            self.indexer.add_embeddings(embedding_results)
            try:
                self.indexer.save_index()
            except Exception as e:
                logger.error(f"Failed to persist index after batch: {e}")
                raise
        return len(embedding_results)
    
    
    def get_indexing_stats(self, project_path: str) -> Optional[Dict]:
        """Get indexing statistics for a project.
        
        Args:
            project_path: Path to project
            
        Returns:
            Dictionary with statistics or None
        """
        metadata = self.snapshot_manager.load_metadata(project_path)
        if not metadata:
            return None
        
        # Add current index stats
        metadata['current_chunks'] = self.indexer.get_index_size()
        metadata['snapshot_age'] = self.snapshot_manager.get_snapshot_age(project_path)
        
        return metadata
    
    def needs_reindex(self, project_path: str, max_age_minutes: float = 5) -> bool:
        """Check if a project needs reindexing.
        
        Args:
            project_path: Path to project
            max_age_minutes: Maximum age of snapshot in minutes (default 5)
            
        Returns:
            True if reindex is needed
        """
        # No snapshot means needs index
        if not self.snapshot_manager.has_snapshot(project_path):
            return True
        
        # Check snapshot age (convert minutes to seconds)
        age = self.snapshot_manager.get_snapshot_age(project_path)
        if age and age > max_age_minutes * 60:
            return True
        
        # Quick check for changes
        return self.change_detector.quick_check(project_path)
    
    def auto_reindex_if_needed(self, project_path: str, project_name: Optional[str] = None, 
                              max_age_minutes: float = 5) -> IncrementalIndexResult:
        """Automatically reindex if the index is stale.
        
        Args:
            project_path: Path to project
            project_name: Optional project name
            max_age_minutes: Maximum age before auto-reindex (default 5 minutes)
            
        Returns:
            IncrementalIndexResult with statistics
        """
        import time
        start_time = time.time()
        
        if self.needs_reindex(project_path, max_age_minutes):
            logger.info(f"Auto-reindexing {project_path} (index older than {max_age_minutes} minutes)")
            return self.incremental_index(project_path, project_name)
        else:
            logger.debug(f"Index for {project_path} is fresh, skipping reindex")
            return IncrementalIndexResult(
                files_added=0,
                files_removed=0,
                files_modified=0,
                chunks_added=0,
                chunks_removed=0,
                time_taken=time.time() - start_time,
                success=True
            )
