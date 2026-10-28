"""Admin API: manage the agent's Knowledge, Manuals, and Memory from your
CRM backend (or any HTTP client).

    export ADMIN_API_TOKEN=$(openssl rand -hex 24)
    python admin_api.py        # listens on 127.0.0.1:8081 by default

Endpoints (all JSON unless noted; every request must carry the token as
``Authorization: Bearer <ADMIN_API_TOKEN>`` or ``X-Admin-Token``):

    GET    /admin/status                    agent config + store overview
    GET    /admin/{store}                   list files      (store: knowledge | manuals | memory)
    GET    /admin/{store}/{path}            download one file (raw bytes)
    PUT    /admin/{store}/{path}            upload/replace one file (raw request body)
    DELETE /admin/{store}/{path}            delete one file

Design notes:

- **No reload endpoint is needed.** Knowledge and the manual index detect
  directory changes automatically: an uploaded knowledge file applies to
  the next new conversation, an uploaded manual is re-indexed on the next
  search, and memory files are read per command. Ongoing conversations
  keep their snapshot (prompt-cache stability).
- **Bind to localhost.** Default bind is 127.0.0.1 - on the same server,
  your CRM backend calls http://127.0.0.1:8081 directly and nothing is
  exposed publicly. Set ADMIN_API_BIND=0.0.0.0 only behind a firewall or
  reverse proxy that restricts access.
- Uploads are extension-validated per store and size-capped
  (ADMIN_MAX_UPLOAD_BYTES, default 25 MB). Paths may nest one directory
  deep and are hard-confined to each store's directory.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import agent_stores
import catalogue
import knowledge
import memory_store
import retrieval

BIND = os.environ.get("ADMIN_API_BIND", "127.0.0.1")
PORT = int(os.environ.get("ADMIN_API_PORT", "8081"))
MAX_UPLOAD_BYTES = int(os.environ.get("ADMIN_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

DOC_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".pdf"}
MEMORY_EXTENSIONS = {".md", ".txt", ".json", ".csv"}

CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".pdf": "application/pdf",
}


def stores() -> dict[str, dict]:
    """Resolved at call time so env overrides (and tests) take effect."""
    return {
        "knowledge": {"dir": knowledge.knowledge_dir(), "extensions": DOC_EXTENSIONS,
                      "note": "inlined into the system prompt of each NEW conversation"},
        "manuals": {"dir": retrieval.manuals_dir(), "extensions": DOC_EXTENSIONS,
                    "note": "chunked + BM25-indexed; searched on demand"},
        "memory": {"dir": memory_store.memory_dir(), "extensions": MEMORY_EXTENSIONS,
                   "note": "the agent's own notes; edit carefully"},
    }


def resolve_in_store(store: dict, rel_path: str) -> Path | None:
    """Confine a URL path to the store directory (one nesting level allowed).

    In a shared store the ``agents/`` folder is reserved for per-agent files,
    which are only reachable with ``?agent=<slug>`` - so a shared upload can
    never land in, or overwrite, an agent's private folder."""
    root = store["dir"].resolve()
    cleaned = rel_path.strip("/")
    if not cleaned or cleaned.count("/") > 1:
        return None
    if store.get("shared") and cleaned.split("/")[0] == agent_stores.AGENTS_DIRNAME:
        return None
    candidate = (root / cleaned).resolve()
    if candidate == root or (root not in candidate.parents):
        return None
    return candidate


def _log(text: str) -> None:
    print(f"[admin] {text}", file=sys.stderr)


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token: str):
        super().__init__(address, AdminHandler)
        self.token = token


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminServer

    def log_message(self, *args) -> None:
        pass

    # -- plumbing ---------------------------------------------------------------

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Admin-Token", "")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer "):]
        return bool(self.server.token) and hmac.compare_digest(supplied, self.server.token)

    def _route(self):
        """-> (store_name, store, rel_path) or None after responding."""
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) < 2 or parts[0] != "admin":
            self._send_json(404, {"error": "not found"})
            return None
        if parts[1] == "status":
            return ("status", None, "")
        all_stores = stores()
        if parts[1] not in all_stores:
            self._send_json(404, {"error": f"unknown store '{parts[1]}'; use knowledge, manuals, or memory"})
            return None
        store = dict(all_stores[parts[1]])
        slug = self._agent_slug()
        if slug:
            # This agent's private folder; created on first upload.
            store["dir"] = agent_stores.agent_dir(store["dir"], slug)
            store["agent"] = slug
            store["shared"] = False
        else:
            store["shared"] = True
        return (parts[1], store, "/".join(parts[2:]))

    def _agent_slug(self) -> str | None:
        """``?agent=<slug>`` selects an agent's own folder; absent means the
        shared store every agent reads."""
        query = parse_qs(self.path.partition("?")[2])
        return agent_stores.safe_slug((query.get("agent") or [""])[0])

    # -- store operations --------------------------------------------------------

    @staticmethod
    def _list_store(name: str, store: dict) -> dict:
        root = store["dir"]
        files = []
        reserved = root / agent_stores.AGENTS_DIRNAME if store.get("shared", True) else None
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if reserved is not None and (path == reserved or reserved in path.parents):
                    continue  # other agents' private files are not shared content
                if path.is_file():
                    stat = path.stat()
                    files.append({
                        "name": str(path.relative_to(root)),
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    })
        return {"store": name, "directory": str(root), "note": store["note"],
                "agent": store.get("agent"),
                "extensions": sorted(store["extensions"]), "files": files}

    def _status(self) -> dict:
        slug = self._agent_slug()
        overview = {}
        for name, s in stores().items():
            shared = {**s, "shared": True}
            entry = {"files": len(self._list_store(name, shared)["files"]),
                     "directory": str(s["dir"])}
            if slug:
                own = {**s, "dir": agent_stores.agent_dir(s["dir"], slug), "shared": False}
                entry["agent_files"] = len(self._list_store(name, own)["files"])
                entry["agent_directory"] = str(own["dir"])
            overview[name] = entry
        # The index this agent actually searches: shared manuals plus its own.
        index = retrieval.get_index(slug)
        return {
            "agent": {
                "model": os.environ.get("CALL_CENTER_MODEL", "claude-opus-5"),
                "effort": os.environ.get("CALL_CENTER_EFFORT", "medium"),
                "crm": "rest" if os.environ.get("CRM_API_BASE_URL") else "mock",
                "trengo_configured": bool(os.environ.get("TRENGO_API_KEY")),
            },
            "agent_slug": slug,
            "stores": overview,
            "manual_index": {"chunks": len(index.chunks),
                             "sources": sorted({c.source for c in index.chunks})},
        }

    # -- HTTP methods ------------------------------------------------------------

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if not self._authorized():
            self._send_json(401, {"error": "bad or missing admin token"})
            return
        if self._catalogue_parts() is not None:
            self._catalogue_get()
            return
        routed = self._route()
        if routed is None:
            return
        name, store, rel_path = routed
        if name == "status":
            self._send_json(200, self._status())
            return
        if not rel_path:
            self._send_json(200, self._list_store(name, store))
            return
        target = resolve_in_store(store, rel_path)
        if target is None or not target.is_file():
            self._send_json(404, {"error": f"no such file in {name}: {rel_path}"})
            return
        self._send_bytes(target.read_bytes(),
                         CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))

    def do_PUT(self):
        if not self._authorized():
            self._send_json(401, {"error": "bad or missing admin token"})
            return
        routed = self._route()
        if routed is None:
            return
        name, store, rel_path = routed
        if name == "status" or not rel_path:
            self._send_json(405, {"error": "PUT a file path, e.g. /admin/knowledge/hours.md"})
            return
        target = resolve_in_store(store, rel_path)
        if target is None:
            self._send_json(400, {"error": "invalid path"})
            return
        if target.suffix.lower() not in store["extensions"]:
            self._send_json(415, {"error": f"{name} accepts: {', '.join(sorted(store['extensions']))}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_json(413, {"error": f"file exceeds {MAX_UPLOAD_BYTES} bytes"})
            return
        data = self.rfile.read(length)
        target.parent.mkdir(parents=True, exist_ok=True)  # an agent's folder appears on first upload
        target.write_bytes(data)
        _log(f"{name}/{rel_path} uploaded ({length} bytes)")
        self._send_json(200, {"saved": True, "store": name, "name": rel_path, "size": length,
                              "applies": stores()[name]["note"]})

    def do_DELETE(self):
        if not self._authorized():
            self._send_json(401, {"error": "bad or missing admin token"})
            return
        parts = self._catalogue_parts()
        if parts is not None:
            if len(parts) != 1:
                self._send_json(405, {"error": "DELETE /admin/catalogue/{id}"})
                return
            self._send_json(200, {"forgotten": catalogue.forget(parts[0]), "id": parts[0]})
            return
        routed = self._route()
        if routed is None:
            return
        name, store, rel_path = routed
        if name == "status" or not rel_path:
            self._send_json(405, {"error": "DELETE a file path, e.g. /admin/manuals/old.pdf"})
            return
        target = resolve_in_store(store, rel_path)
        if target is None or not target.is_file():
            self._send_json(404, {"error": f"no such file in {name}: {rel_path}"})
            return
        target.unlink()
        _log(f"{name}/{rel_path} deleted")
        self._send_json(200, {"deleted": True, "store": name, "name": rel_path})


    # -- catalogues ---------------------------------------------------------------
    # The CRM configures an agent's catalogues; this process fetches and keeps
    # them. Only counts, freshness and detected fields come back - the items
    # themselves never cross into the CRM.

    def _catalogue_parts(self) -> list[str] | None:
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "admin" and parts[1] == "catalogue":
            return parts[2:]
        return None

    def _catalogue_get(self) -> None:
        parts = self._catalogue_parts() or []
        if len(parts) != 1:
            self._send_json(404, {"error": "GET /admin/catalogue/{id}"})
            return
        self._send_json(200, catalogue.stats(parts[0]))

    def _json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:
            self._send_json(400, {"error": "expected a small JSON body"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self._send_json(400, {"error": "body must be JSON"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return payload

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "bad or missing admin token"})
            return
        parts = self._catalogue_parts()
        if parts is None:
            self._send_json(404, {"error": "POST is only used for /admin/catalogue/..."})
            return
        payload = self._json_body()
        if payload is None:
            return

        url = str(payload.get("feed_url") or "").strip()
        item_element = str(payload.get("item_element") or "").strip() or None

        # POST /admin/catalogue/scan - preview a feed before it is saved.
        if parts == ["scan"]:
            try:
                parsed = catalogue.parse_feed(catalogue.fetch_feed(url), item_element)
            except catalogue.CatalogueError as exc:
                self._send_json(422, {"error": str(exc)})
                return
            samples = [{k: v for k, v in item.items() if k != "attributes"} for item in parsed["items"][:3]]
            self._send_json(200, {
                "item_tag": parsed["item_tag"],
                "item_count": len(parsed["items"]),
                "fields": parsed["fields"],
                "dropped_price_fields": parsed["dropped_price_fields"],
                "samples": samples,
            })
            return

        # POST /admin/catalogue/{id}/sync - fetch now and keep the snapshot.
        if len(parts) == 2 and parts[1] == "sync":
            snapshot = catalogue.sync({
                "id": parts[0], "name": payload.get("name"), "feed_url": url,
                "item_element": item_element, "refresh_minutes": payload.get("refresh_minutes"),
            })
            status = 200 if not snapshot.get("last_error") else 422
            self._send_json(status, {**catalogue.stats(parts[0]), "error": snapshot.get("last_error")})
            return

        self._send_json(404, {"error": "use /admin/catalogue/scan or /admin/catalogue/{id}/sync"})


def main() -> None:
    token = os.environ.get("ADMIN_API_TOKEN", "").strip()
    if not token:
        print("ADMIN_API_TOKEN is not set. Generate one: openssl rand -hex 24", file=sys.stderr)
        raise SystemExit(1)
    server = AdminServer((BIND, PORT), token)
    _log(f"admin API listening on {BIND}:{PORT} (token required)")
    server.serve_forever()


if __name__ == "__main__":
    main()
