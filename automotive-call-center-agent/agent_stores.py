"""Where each agent's files live.

Every store keeps its existing flat directory as the **shared** set that every
agent reads — dealership hours apply to all of them — and gains a per-agent
subdirectory for files only one agent should see:

    knowledge/                     shared, what was always there
    knowledge/agents/<slug>/       that agent's own

Nothing moves on disk, so an install that predates agents keeps working and an
agent with no folder of its own simply reads the shared set. Resolution is
deliberately boring: a slug is slugified again here, so a path can never climb
out of the store directory however it reached us.
"""

from __future__ import annotations

import re
from pathlib import Path

AGENTS_DIRNAME = "agents"

_SAFE_SLUG = re.compile(r"[^a-z0-9-]+")


def safe_slug(slug: str | None) -> str | None:
    """A slug reduced to what may appear in a directory name, or None."""
    if not slug:
        return None
    cleaned = _SAFE_SLUG.sub("-", str(slug).strip().lower()).strip("-")
    return cleaned or None


def agent_dir(root: Path, slug: str | None) -> Path | None:
    """The per-agent subdirectory inside a store, or None when no agent is in
    play. Not created here — a store only exists once something is put in it."""
    cleaned = safe_slug(slug)
    if not cleaned:
        return None
    return root / AGENTS_DIRNAME / cleaned


def search_dirs(root: Path, slug: str | None) -> list[Path]:
    """Directories to read for this agent, shared first then its own, so the
    agent's own files are the later (more specific) word on a subject."""
    dirs = [root]
    own = agent_dir(root, slug)
    if own is not None:
        dirs.append(own)
    return [d for d in dirs if d.is_dir()]


def iter_files(root: Path, slug: str | None, suffixes: set[str]) -> list[tuple[str, Path]]:
    """(display name, path) for every readable file this agent can see.

    The shared directory is read non-recursively so the ``agents/`` tree is not
    swept up as shared content; a per-agent directory is read the same way.
    Per-agent names are prefixed so two agents' files never collide in output.
    """
    found: list[tuple[str, Path]] = []

    for path in sorted(root.iterdir()) if root.is_dir() else []:
        if path.is_file() and path.suffix.lower() in suffixes:
            found.append((path.name, path))

    own = agent_dir(root, slug)
    if own is not None and own.is_dir():
        for path in sorted(own.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                found.append((str(path.relative_to(own)), path))

    return found


def signature(root: Path, slug: str | None, suffixes: set[str]) -> tuple:
    """Cache key: rebuilds when anything this agent can see changes."""
    return tuple(
        (name, path.stat().st_mtime_ns, path.stat().st_size)
        for name, path in iter_files(root, slug, suffixes)
    )
