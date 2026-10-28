"""Retrieval over full service manuals for the call center agent.

Where ``knowledge/`` documents are small enough to inline into the (cached)
system prompt, service manuals are far too large for that. Instead, files
in ``manuals/`` (or ``MANUALS_DIR``) are split into chunks and indexed with
BM25 at startup; the agent searches the index on demand through the
``search_service_manuals`` tool and only the few matching chunks enter the
conversation.

The index is lexical (BM25) and pure Python - no embedding service, vector
database, or network dependency - which is a strong fit for manual lookups
("lug nut torque", "coolant capacity", "brake warning lamp"). To upgrade to
semantic retrieval later, replace ``BM25Index`` with an embedding store
(e.g. Voyage AI embeddings); the chunker, tool, and prompt stay the same.

Supported formats mirror the knowledge loader: .md/.txt/.csv/.json as text,
.pdf via pypdf (chunks remember their page numbers).
"""

from __future__ import annotations

import math
import os
import re
import sys

import agent_stores
from dataclasses import dataclass
from pathlib import Path

# Chunk geometry: ~1600 chars (roughly 400 tokens) reads as a coherent
# manual passage; overlap keeps facts that straddle a boundary findable.
CHUNK_CHARS = 1600
CHUNK_OVERLAP = 200

_WORD = re.compile(r"[a-z0-9]+")

# Minimal stopword list - enough to stop glue words from dominating scores.
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have how in is it its of on or "
    "that the this to was what when where which will with your you".split()
)


def manuals_dir() -> Path:
    configured = os.environ.get("MANUALS_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "manuals"


def _tokenize(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOPWORDS]


@dataclass
class Chunk:
    source: str    # file name
    section: str   # nearest heading, or "page N" for PDFs
    text: str


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown/plain text into (heading, body) sections."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append((match.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    return [(head, "\n".join(lines).strip()) for head, lines in sections if "\n".join(lines).strip()]


def _window(text: str) -> list[str]:
    """Split one section body into overlapping windows of CHUNK_CHARS,
    breaking on whitespace where possible."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    windows = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            space = text.rfind(" ", start + CHUNK_CHARS // 2, end)
            if space != -1:
                end = space
        windows.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [w for w in windows if w]


def _chunk_text_file(path: Path) -> list[Chunk]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[manuals] skipping {path.name}: {exc}", file=sys.stderr)
        return []
    chunks = []
    for heading, body in _split_sections(text):
        labeled = f"{heading}\n{body}" if heading else body
        for window in _window(labeled):
            chunks.append(Chunk(source=path.name, section=heading or "(no heading)", text=window))
    return chunks


def _chunk_pdf(path: Path) -> list[Chunk]:
    try:
        from pypdf import PdfReader
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        print(f"[manuals] skipping {path.name}: pypdf unavailable ({exc.__class__.__name__})", file=sys.stderr)
        return []
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        print(f"[manuals] skipping {path.name}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return []
    chunks = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            continue
        for window in _window(text) if text else []:
            chunks.append(Chunk(source=path.name, section=f"page {number}", text=window))
    return chunks


MANUAL_SUFFIXES = {".pdf", ".md", ".txt", ".csv", ".json"}


def load_chunks(agent_slug: str | None = None) -> list[Chunk]:
    """Chunks from every manual this agent can see: the shared manuals
    directory, then its own ``agents/<slug>/`` folder."""
    chunks: list[Chunk] = []
    for _name, path in agent_stores.iter_files(manuals_dir(), agent_slug, MANUAL_SUFFIXES):
        if path.suffix.lower() == ".pdf":
            chunks.extend(_chunk_pdf(path))
        else:
            chunks.extend(_chunk_text_file(path))
    return chunks


# ---------------------------------------------------------------------------
# BM25 index
# ---------------------------------------------------------------------------

class BM25Index:
    """Classic BM25 (k1=1.5, b=0.75) over the manual chunks."""

    K1 = 1.5
    B = 0.75

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._term_freqs: list[dict[str, int]] = []
        self._doc_lens: list[int] = []
        doc_freq: dict[str, int] = {}
        for chunk in chunks:
            tokens = _tokenize(f"{chunk.section} {chunk.text}")
            freqs: dict[str, int] = {}
            for token in tokens:
                freqs[token] = freqs.get(token, 0) + 1
            self._term_freqs.append(freqs)
            self._doc_lens.append(len(tokens))
            for token in freqs:
                doc_freq[token] = doc_freq.get(token, 0) + 1
        n = max(len(chunks), 1)
        self._avg_len = (sum(self._doc_lens) / n) if self._doc_lens else 0.0
        self._idf = {
            token: math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for token, df in doc_freq.items()
        }

    def search(self, query: str, k: int = 4) -> list[tuple[float, Chunk]]:
        terms = _tokenize(query)
        if not terms or not self.chunks:
            return []
        scored = []
        for i, freqs in enumerate(self._term_freqs):
            score = 0.0
            for term in terms:
                tf = freqs.get(term)
                if not tf:
                    continue
                idf = self._idf.get(term, 0.0)
                norm = self.K1 * (1 - self.B + self.B * self._doc_lens[i] / (self._avg_len or 1))
                score += idf * tf * (self.K1 + 1) / (tf + norm)
            if score > 0:
                scored.append((score, self.chunks[i]))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:k]


# One index per agent (None = the shared manuals alone), each rebuilt only
# when something that agent can see changes on disk.
_indexes: dict[str | None, tuple[tuple, BM25Index]] = {}


def _dir_signature(agent_slug: str | None = None) -> tuple:
    return agent_stores.signature(manuals_dir(), agent_slug, MANUAL_SUFFIXES)


def get_index(agent_slug: str | None = None) -> BM25Index:
    """Build an agent's index on first use and rebuild it automatically
    whenever its manuals change on disk (e.g. a manual uploaded through the
    admin API) - no restart or signaling needed."""
    key = agent_stores.safe_slug(agent_slug)
    signature = _dir_signature(key)
    cached = _indexes.get(key)
    if cached is None or cached[0] != signature:
        chunks = load_chunks(key)
        cached = (signature, BM25Index(chunks))
        _indexes[key] = cached
        if chunks:
            sources = sorted({c.source for c in chunks})
            label = f" for {key}" if key else ""
            print(f"[manuals] indexed {len(chunks)} chunks{label} from: {', '.join(sources)}", file=sys.stderr)
    return cached[1]


def reset_index() -> None:
    """Drop every cached index (tests)."""
    _indexes.clear()
