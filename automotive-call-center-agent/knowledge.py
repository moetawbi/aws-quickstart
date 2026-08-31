"""Knowledge base loader for the automotive call center agent.

Drop reference documents into the ``knowledge/`` directory (or the
directory named by ``KNOWLEDGE_DIR``) and the agent loads them at startup:
dealership hours, locations, financing FAQs, promotion sheets, loaner and
shuttle policies, seasonal service campaigns, and so on.

Supported file types: ``.md``, ``.txt``, ``.csv``, ``.json`` (read as
text) and ``.pdf`` (text extracted with pypdf). Files are concatenated
into one system block that sits under the prompt-cache breakpoint, so the
whole knowledge base is read from cache on every turn after the first -
it must therefore stay byte-stable for the life of the process (edits are
picked up on restart).

Size guards keep the context sane: oversized files are truncated with a
visible marker and the total is capped. For a knowledge base too large
for these caps, move to retrieval (a search tool over a vector store)
instead of inlining - see the README.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SUPPORTED_TEXT = {".md", ".txt", ".csv", ".json"}
SUPPORTED = SUPPORTED_TEXT | {".pdf"}

# ~100k chars is roughly 25k-30k tokens; generous for policy docs while
# leaving plenty of context for the call itself.
MAX_CHARS_PER_FILE = int(os.environ.get("KNOWLEDGE_MAX_CHARS_PER_FILE", "100000"))
MAX_CHARS_TOTAL = int(os.environ.get("KNOWLEDGE_MAX_CHARS_TOTAL", "400000"))

TRUNCATION_MARKER = "\n[... truncated: file exceeds the knowledge size limit ...]"


def knowledge_dir() -> Path:
    configured = os.environ.get("KNOWLEDGE_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "knowledge"


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        # Not just ImportError: a broken transitive dependency (e.g. the
        # system `cryptography` package) can fail with exotic exceptions.
        # PDFs are optional knowledge - degrade to a warning, never crash.
        print(
            f"[knowledge] skipping {path.name}: pypdf unavailable "
            f"({exc.__class__.__name__}) - install/repair pypdf to load PDFs",
            file=sys.stderr,
        )
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as exc:  # a corrupt PDF must not kill the call center
        print(f"[knowledge] skipping {path.name}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return ""


def _read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        print(f"[knowledge] skipping {path.name}: {exc}", file=sys.stderr)
        return ""
    if path.suffix.lower() == ".json":
        # Re-serialize deterministically so the cached prefix is stable
        # even if the file is regenerated with different whitespace.
        try:
            text = json.dumps(json.loads(text), indent=2, sort_keys=True)
        except ValueError:
            pass  # not valid JSON - keep the raw text
    return text


def load_documents() -> list[tuple[str, str]]:
    """Load (filename, text) pairs from the knowledge directory, sorted by
    name for a deterministic - and therefore cacheable - ordering."""
    directory = knowledge_dir()
    if not directory.is_dir():
        return []
    documents = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        text = _read_pdf(path) if path.suffix.lower() == ".pdf" else _read_text(path)
        if not text:
            continue
        if len(text) > MAX_CHARS_PER_FILE:
            text = text[:MAX_CHARS_PER_FILE] + TRUNCATION_MARKER
            print(f"[knowledge] {path.name} truncated to {MAX_CHARS_PER_FILE} chars", file=sys.stderr)
        documents.append((path.name, text))
    return documents


def load_knowledge_text() -> str:
    """The knowledge base as one formatted string for a system block, or
    an empty string when there is nothing to load."""
    documents = load_documents()
    if not documents:
        return ""
    sections = []
    total = 0
    for name, text in documents:
        section = f'<document name="{name}">\n{text}\n</document>'
        if total + len(section) > MAX_CHARS_TOTAL:
            print(f"[knowledge] total size cap reached; skipping {name} and later files", file=sys.stderr)
            break
        sections.append(section)
        total += len(section)
    if not sections:
        return ""
    loaded = ", ".join(name for name, _ in documents[: len(sections)])
    print(f"[knowledge] loaded {len(sections)} document(s): {loaded}", file=sys.stderr)
    return (
        "# Dealership knowledge base\n\n"
        "The following reference documents were provided by the dealership. "
        "Treat them as authoritative for general questions (hours, policies, "
        "promotions, directions). Account-specific facts still come from the "
        "tools, never from these documents.\n\n" + "\n\n".join(sections)
    )
