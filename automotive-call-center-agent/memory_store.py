"""Persistent memory for the call center agent (Anthropic memory tool).

Implements the ``memory_20250818`` tool with a filesystem backend: Claude
itself decides what is worth remembering during conversations (phone or
Trengo) and reads/writes files under ``memory/`` (or ``MEMORY_DIR``)
through view/create/str_replace/insert/delete/rename commands. The
directory persists across conversations, workers, and restarts, so what
the agent learns on one Trengo ticket is available on the next call.

Safety properties:

- All paths are confined to the memory root: Claude addresses files as
  ``/memories/...`` and every path is resolved and checked against the
  root, so neither a confused model nor a prompt-injected customer message
  can read or write outside it.
- Size caps (per file, total, file count) keep memory from growing into
  a context or disk problem.
- A process-wide lock serializes access (the Trengo workers already
  process tickets serially; the lock covers any future concurrency).
- Errors are returned as tool-result strings, never raised, so a bad
  command degrades conversationally.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from anthropic.lib.tools import BetaAbstractMemoryTool

MAX_FILE_BYTES = int(os.environ.get("MEMORY_MAX_FILE_BYTES", "100000"))
MAX_TOTAL_BYTES = int(os.environ.get("MEMORY_MAX_TOTAL_BYTES", "2000000"))
MAX_FILES = int(os.environ.get("MEMORY_MAX_FILES", "200"))
VIRTUAL_ROOT = "/memories"


def memory_dir() -> Path:
    configured = os.environ.get("MEMORY_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "memory"


class FileMemoryTool(BetaAbstractMemoryTool):
    """Filesystem-backed memory rooted at ``memory_dir()``."""

    def __init__(self, root: Path | None = None):
        super().__init__()
        self.root = (root or memory_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- path & size guards ----------------------------------------------------

    def _resolve(self, path: str) -> Path | None:
        """Map a tool path (``/memories/foo.md``) to a real path inside the
        root; None if it would escape."""
        cleaned = str(path or "").strip()
        if cleaned.startswith(VIRTUAL_ROOT):
            cleaned = cleaned[len(VIRTUAL_ROOT):]
        elif cleaned.startswith("/"):
            return None  # absolute paths must live under /memories
        cleaned = cleaned.lstrip("/")
        candidate = (self.root / cleaned).resolve() if cleaned else self.root
        if candidate != self.root and self.root not in candidate.parents:
            return None
        return candidate

    def _usage(self) -> tuple[int, int]:
        files = [p for p in self.root.rglob("*") if p.is_file()]
        return len(files), sum(p.stat().st_size for p in files)

    def _write_allowed(self, target: Path, new_bytes: int) -> str | None:
        if new_bytes > MAX_FILE_BYTES:
            return f"Error: file would be {new_bytes} bytes; the per-file limit is {MAX_FILE_BYTES}."
        count, total = self._usage()
        existing = target.stat().st_size if target.exists() else 0
        if target.exists() is False and count + 1 > MAX_FILES:
            return f"Error: memory already holds {count} files (limit {MAX_FILES}). Delete or consolidate first."
        if total - existing + new_bytes > MAX_TOTAL_BYTES:
            return f"Error: memory is full ({total} bytes, limit {MAX_TOTAL_BYTES}). Delete or consolidate first."
        return None

    @staticmethod
    def _numbered(text: str, start: int = 1) -> str:
        lines = text.split("\n")
        return "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=start))

    # -- commands ---------------------------------------------------------------

    def view(self, command) -> str:
        with self._lock:
            target = self._resolve(command.path)
            if target is None:
                return f"Error: path {command.path} is outside the memory directory."
            if target.is_dir():
                entries = sorted(target.rglob("*"))
                if not entries:
                    return f"Directory {command.path or VIRTUAL_ROOT} is empty."
                listing = []
                for entry in entries:
                    rel = entry.relative_to(self.root)
                    suffix = "/" if entry.is_dir() else f" ({entry.stat().st_size} bytes)"
                    listing.append(f"{VIRTUAL_ROOT}/{rel}{suffix}")
                return "\n".join(listing)
            if not target.is_file():
                return f"Error: {command.path} does not exist."
            text = target.read_text(encoding="utf-8", errors="replace")
            view_range = getattr(command, "view_range", None)
            if view_range and len(view_range) == 2:
                lines = text.split("\n")
                start = max(1, view_range[0])
                end = min(len(lines), view_range[1])
                return self._numbered("\n".join(lines[start - 1:end]), start=start)
            return self._numbered(text)

    def create(self, command) -> str:
        with self._lock:
            target = self._resolve(command.path)
            if target is None:
                return f"Error: path {command.path} is outside the memory directory."
            if target == self.root:
                return "Error: provide a file path, not the memory root."
            data = command.file_text or ""
            problem = self._write_allowed(target, len(data.encode("utf-8")))
            if problem:
                return problem
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data, encoding="utf-8")
            return f"File {command.path} written ({len(data.encode('utf-8'))} bytes)."

    def str_replace(self, command) -> str:
        with self._lock:
            target = self._resolve(command.path)
            if target is None or not target.is_file():
                return f"Error: {command.path} does not exist."
            text = target.read_text(encoding="utf-8", errors="replace")
            occurrences = text.count(command.old_str)
            if occurrences == 0:
                return "Error: old_str not found in the file."
            if occurrences > 1:
                return f"Error: old_str appears {occurrences} times; include more context so it is unique."
            new_text = text.replace(command.old_str, command.new_str, 1)
            problem = self._write_allowed(target, len(new_text.encode("utf-8")))
            if problem:
                return problem
            target.write_text(new_text, encoding="utf-8")
            return f"File {command.path} updated."

    def insert(self, command) -> str:
        with self._lock:
            target = self._resolve(command.path)
            if target is None or not target.is_file():
                return f"Error: {command.path} does not exist."
            lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
            at = command.insert_line
            if at < 0 or at > len(lines):
                return f"Error: insert_line {at} is out of range (file has {len(lines)} lines)."
            lines[at:at] = command.insert_text.split("\n")
            new_text = "\n".join(lines)
            problem = self._write_allowed(target, len(new_text.encode("utf-8")))
            if problem:
                return problem
            target.write_text(new_text, encoding="utf-8")
            return f"Inserted at line {at} in {command.path}."

    def delete(self, command) -> str:
        with self._lock:
            target = self._resolve(command.path)
            if target is None:
                return f"Error: path {command.path} is outside the memory directory."
            if target == self.root:
                return "Error: cannot delete the memory root."
            if target.is_dir():
                shutil.rmtree(target)
                return f"Directory {command.path} deleted."
            if target.is_file():
                target.unlink()
                return f"File {command.path} deleted."
            return f"Error: {command.path} does not exist."

    def rename(self, command) -> str:
        with self._lock:
            source = self._resolve(command.old_path)
            destination = self._resolve(command.new_path)
            if source is None or destination is None:
                return "Error: paths must stay inside the memory directory."
            if not source.exists():
                return f"Error: {command.old_path} does not exist."
            if destination.exists():
                return f"Error: {command.new_path} already exists."
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            return f"Renamed {command.old_path} to {command.new_path}."

    def clear_all_memory(self) -> str:
        with self._lock:
            for entry in self.root.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            return "All memory cleared."
