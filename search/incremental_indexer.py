"""Incremental indexing using Merkle tree change detection."""

import logging
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common_utils import project_context
from merkle.change_detector import ChangeDetector, FileChanges
from merkle.merkle_dag import MerkleDAG
from merkle.snapshot_manager import SnapshotManager
from chunking.multi_language_chunker import MultiLanguageChunker
from chunking.parallel import chunk_one
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
DEFAULT_FLUSH_CHUNKS = 2000              # bound embedding-batch peak memory
DEFAULT_FLUSH_BYTES = 256 * 1024 * 1024  # 256 MB of chunked content
DEFAULT_FLUSH_SECONDS = 60.0

# Checkpoint cadence: how often we rewrite the FAISS index file (expensive).
# Per-flush metadata commits remain cheap and run on every flush.
DEFAULT_CHECKPOINT_CHUNKS = 50_000
DEFAULT_CHECKPOINT_SECONDS = 600.0       # 10 minutes

# Chunking parallelism (one worker process per CPU, capped).
DEFAULT_CHUNK_WORKERS = max(1, min(8, (os.cpu_count() or 2) - 1))

# Periodic progress log cadence during a long-running indexing pass.
DEFAULT_PROGRESS_SECONDS = 60.0


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
        flush_every_chunks: Optional[int] = None,
        flush_every_bytes: Optional[int] = None,
        flush_every_seconds: Optional[float] = None,
        checkpoint_every_chunks: Optional[int] = None,
        checkpoint_every_seconds: Optional[float] = None,
        n_chunk_workers: Optional[int] = None,
        progress_every_seconds: Optional[float] = None,
    ):
        """Initialize incremental indexer.

        Args:
            indexer: Indexer instance
            embedder: Embedder instance
            chunker: Code chunker instance
            snapshot_manager: Snapshot manager instance
            flush_every_files: Flush after this many files (env CODE_SEARCH_FLUSH_FILES)
            flush_every_chunks: Flush after the buffer has this many chunks
                (env CODE_SEARCH_FLUSH_CHUNKS). Bounds peak embedding-batch memory.
            flush_every_bytes: Flush after this many bytes of chunked content
                (env CODE_SEARCH_FLUSH_BYTES)
            flush_every_seconds: Flush after this many seconds since the last flush
                (env CODE_SEARCH_FLUSH_SECONDS)
            checkpoint_every_chunks: Rewrite FAISS index file after this many newly
                indexed chunks (env CODE_SEARCH_CHECKPOINT_CHUNKS).
            checkpoint_every_seconds: Rewrite FAISS index file after this many seconds
                (env CODE_SEARCH_CHECKPOINT_SECONDS).
            n_chunk_workers: Number of parallel chunker processes; 1 disables
                parallelism (env CODE_SEARCH_CHUNK_WORKERS).
            progress_every_seconds: Emit a progress log line at most this
                often during indexing (env CODE_SEARCH_PROGRESS_SECONDS,
                default 60). Set to 0 to disable.
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
        self.flush_every_chunks = (
            flush_every_chunks
            if flush_every_chunks is not None
            else _env_int("CODE_SEARCH_FLUSH_CHUNKS", DEFAULT_FLUSH_CHUNKS)
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
        self.checkpoint_every_chunks = (
            checkpoint_every_chunks
            if checkpoint_every_chunks is not None
            else _env_int("CODE_SEARCH_CHECKPOINT_CHUNKS", DEFAULT_CHECKPOINT_CHUNKS)
        )
        self.checkpoint_every_seconds = (
            checkpoint_every_seconds
            if checkpoint_every_seconds is not None
            else _env_float("CODE_SEARCH_CHECKPOINT_SECONDS", DEFAULT_CHECKPOINT_SECONDS)
        )
        self.n_chunk_workers = (
            n_chunk_workers
            if n_chunk_workers is not None
            else _env_int("CODE_SEARCH_CHUNK_WORKERS", DEFAULT_CHUNK_WORKERS)
        )
        self.progress_every_seconds = (
            progress_every_seconds
            if progress_every_seconds is not None
            else _env_float("CODE_SEARCH_PROGRESS_SECONDS", DEFAULT_PROGRESS_SECONDS)
        )

        # Checkpoint state — reset by _maybe_checkpoint after each rewrite.
        self._chunks_since_checkpoint = 0
        self._last_checkpoint_time = time.time()

        logger.info(
            "Indexer thresholds: "
            f"flush(files={self.flush_every_files}, "
            f"chunks={self.flush_every_chunks}, "
            f"bytes={self.flush_every_bytes}, "
            f"seconds={self.flush_every_seconds}); "
            f"checkpoint(chunks={self.checkpoint_every_chunks}, "
            f"seconds={self.checkpoint_every_seconds}); "
            f"chunk_workers={self.n_chunk_workers}; "
            f"progress_every_seconds={self.progress_every_seconds}"
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

        # Tag every log record produced under this call with the project name.
        with project_context(project_name):
            return self._incremental_index_inner(
                project_path, project_name, force_full, start_time
            )

    def _incremental_index_inner(
        self,
        project_path: str,
        project_name: str,
        force_full: bool,
        start_time: float,
    ) -> IncrementalIndexResult:
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
            if chunks_removed:
                # Removals only touch SQLite; commit before adds so a crash
                # mid-pass doesn't leave dangling FAISS rows referencing
                # deleted metadata.
                try:
                    self.indexer.commit_metadata()
                except Exception as e:
                    logger.warning(f"Metadata commit after removals failed: {e}")
            chunks_added = self._add_new_chunks(changes, project_path, project_name)

            # Update snapshot
            self.snapshot_manager.save_snapshot(current_dag, {
                'project_name': project_name,
                'incremental_update': True,
                'files_added': len(changes.added),
                'files_removed': len(changes.removed),
                'files_modified': len(changes.modified)
            })

            # Force a final checkpoint so the FAISS file on disk matches the
            # snapshot we just persisted.
            self._maybe_checkpoint(force=True)
            
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

            # Save snapshot only after all batches succeeded. The batched call
            # already force-checkpoints on success, so no extra save is needed.
            self.snapshot_manager.save_snapshot(dag, {
                'project_name': project_name,
                'full_index': True,
                'total_files': len(all_files),
                'supported_files': len(supported_files),
                'chunks_indexed': chunks_added
            })
            
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

        Pipeline:
          1. Chunking is dispatched to ``self.n_chunk_workers`` worker processes
             via ``ProcessPoolExecutor`` (or runs in-process when workers <= 1).
          2. Results stream back into the main thread; chunks accumulate in a
             buffer until any flush threshold trips (files, chunks, bytes, time).
          3. Each flush embeds the buffer and commits metadata cheaply.
          4. The expensive FAISS index file is rewritten only on a checkpoint
             cadence (``checkpoint_every_chunks`` / ``checkpoint_every_seconds``)
             or when forced (final batch / error recovery).

        On exception, flushes whatever is buffered and force-checkpoints before
        re-raising, so partial progress is durable on disk.

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
        total_files = len(supported_files)
        last_progress_time = start

        # Reset checkpoint clock at the start of each indexing pass.
        self._chunks_since_checkpoint = 0
        self._last_checkpoint_time = time.time()

        logger.info(
            f"Indexing pass starting: {total_files} supported files queued"
        )

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

        n_workers = max(1, int(self.n_chunk_workers))
        tasks = [(project_path, f) for f in supported_files]

        # Build the iterator. ProcessPoolExecutor.map streams results in order;
        # chunksize trades off IPC overhead vs. straggler tolerance.
        if n_workers <= 1 or not tasks:
            executor_ctx = nullcontext()
            iterator_factory = lambda: (chunk_one(t) for t in tasks)
            executor = None
        else:
            # Force "spawn" so workers don't inherit a CUDA/torch-initialized
            # parent (which deadlocks on fork). Spawn pays a one-time
            # interpreter-startup cost per worker but is safe.
            mp_ctx = multiprocessing.get_context("spawn")
            executor = ProcessPoolExecutor(max_workers=n_workers, mp_context=mp_ctx)
            executor_ctx = executor
            iterator_factory = lambda: executor.map(chunk_one, tasks, chunksize=8)

        try:
            with executor_ctx:
                for chunks in iterator_factory():
                    files_processed += 1
                    files_in_batch += 1
                    if chunks:
                        chunks_buffer.extend(chunks)
                        bytes_buffer += sum(
                            len(c.content.encode("utf-8", errors="ignore"))
                            for c in chunks
                        )

                    if files_in_batch >= self.flush_every_files:
                        total_chunks += flush("file count")
                    elif len(chunks_buffer) >= self.flush_every_chunks:
                        total_chunks += flush("chunk count")
                    elif bytes_buffer >= self.flush_every_bytes:
                        total_chunks += flush("byte count")
                    elif (time.time() - last_flush_time) >= self.flush_every_seconds:
                        total_chunks += flush("elapsed time")

                    # Periodic progress line (independent of flush cadence).
                    if (
                        self.progress_every_seconds > 0
                        and (time.time() - last_progress_time)
                            >= self.progress_every_seconds
                    ):
                        elapsed = time.time() - start
                        pct = (files_processed / total_files * 100.0) if total_files else 100.0
                        rate = files_processed / elapsed if elapsed > 0 else 0.0
                        remaining = max(0, total_files - files_processed)
                        eta_s = (remaining / rate) if rate > 0 else float("inf")
                        eta_str = (
                            f"{eta_s/60:.1f}m" if eta_s != float("inf") else "?"
                        )
                        logger.info(
                            f"Progress: {files_processed}/{total_files} files "
                            f"({pct:.1f}%) | indexed {total_chunks} chunks | "
                            f"buffered {len(chunks_buffer)} | "
                            f"{rate:.1f} files/s | elapsed {elapsed/60:.1f}m | "
                            f"ETA {eta_str}"
                        )
                        last_progress_time = time.time()

                total_chunks += flush("final")

            # Always force a checkpoint after a successful pass so the FAISS
            # file on disk reflects all flushed work.
            self._maybe_checkpoint(force=True)

            logger.info(
                f"Indexing complete: {total_chunks} chunks from "
                f"{files_processed} files in {time.time() - start:.1f}s "
                f"(workers={n_workers})"
            )
            return total_chunks
        except Exception:
            try:
                total_chunks += flush("error recovery")
                self._maybe_checkpoint(force=True)
                logger.error(
                    f"Indexing aborted after {files_processed} files; "
                    f"flushed {total_chunks} chunks before failure"
                )
            except Exception as inner:
                logger.error(f"Failed to flush during error recovery: {inner}")
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            raise

    def _embed_and_persist_batch(self, chunks: list, project_name: str) -> int:
        """Embed a buffered batch of chunks, add to index, commit metadata.

        FAISS index file rewrite is deferred to ``_maybe_checkpoint``.
        """
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
                self.indexer.commit_metadata()
            except Exception as e:
                logger.error(f"Failed to commit metadata after batch: {e}")
                raise
            self._chunks_since_checkpoint += len(embedding_results)
            self._maybe_checkpoint()
        return len(embedding_results)

    def _maybe_checkpoint(self, force: bool = False) -> None:
        """Rewrite the FAISS index file when the cadence triggers (or forced).

        Honors ``checkpoint_every_chunks`` and ``checkpoint_every_seconds``.
        Resets counters after a successful checkpoint.
        """
        new_chunks = self._chunks_since_checkpoint
        elapsed = time.time() - self._last_checkpoint_time
        if not (
            force
            or new_chunks >= self.checkpoint_every_chunks
            or elapsed >= self.checkpoint_every_seconds
        ):
            return
        if new_chunks == 0 and not force:
            return
        t0 = time.time()
        try:
            self.indexer.checkpoint()
        except Exception as e:
            logger.error(f"FAISS checkpoint failed: {e}")
            raise
        logger.info(
            f"Checkpointed FAISS index ({new_chunks} new chunks, "
            f"{elapsed:.0f}s since last) in {time.time() - t0:.1f}s"
        )
        self._chunks_since_checkpoint = 0
        self._last_checkpoint_time = time.time()
    
    
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
