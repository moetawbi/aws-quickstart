"""Product / vehicle catalogues, configured per agent in the CRM.

Modelled on Dessa's Catalogues: an admin adds a source to an agent - today an
XML feed URL (Google Shopping, Channable, ChannelEngine, a dealer stock feed) -
and the agent can search what is in it. The CRM holds only that
configuration; the items themselves never pass through the CRM database. This
runtime fetches the feed, maps its fields, and keeps a snapshot on disk under
``catalogues/`` so the chat API, the Trengo worker and the admin API (separate
processes) all see the same copy, and a feed outage keeps the last good one.

Two rules are enforced here, at ingest, so no later code path can break them:

- **No prices.** Price-like fields (price, sale_price, cost, msrp, discount,
  monthly payment, currency...) are dropped as the feed is parsed, and money
  amounts written into free text ("Only $18,500!") are masked. The agent hands
  pricing questions to the sales team.
- **Only public sources.** A feed URL must be http(s) and must resolve to a
  public address - checked again on every redirect - so a mistyped or hostile
  URL cannot make this server fetch its own admin API or the office network.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

MAX_FEED_BYTES = int(os.environ.get("CATALOGUE_MAX_FEED_BYTES", str(20 * 1024 * 1024)))
MAX_ITEMS = int(os.environ.get("CATALOGUE_MAX_ITEMS", "20000"))
FETCH_TIMEOUT = float(os.environ.get("CATALOGUE_FETCH_TIMEOUT", "20"))
RETRY_AFTER_FAILURE = float(os.environ.get("CATALOGUE_RETRY_AFTER_SECONDS", "300"))
MAX_REDIRECTS = 3

# Field names carrying money. Matched on the tag's local name, lower-cased.
PRICE_MARKERS = (
    "price", "cost", "msrp", "rrp", "amount", "discount", "payment", "instal",
    "monthly", "vat", "tax", "fee", "currency", "financ", "lease", "deposit", "margin",
)

# Money written into prose: "$18,500", "USD 18500", "18 500 AED", "price: 12000".
_CURRENCY = r"(?:usd|aed|eur|gbp|lbp|ll|sar|qar|kwd|bhd|omr|egp|jod|dollars?|euros?)"
# Every number must end on a digit, so surrounding punctuation and spacing
# survive the mask ("Call for USD 500 deposit" -> "Call for [...] deposit").
_NUMBER = r"\d(?:[\d,. ]*\d)?"
_MONEY = re.compile(
    rf"(?:[$€£¥]\s?{_NUMBER}|\b{_CURRENCY}\b\.?\s?{_NUMBER}"
    rf"|\b{_NUMBER}\s?(?:[$€£]|\b{_CURRENCY}\b)|\bprice\s*[:=\-]?\s*{_NUMBER})",
    re.IGNORECASE,
)

# Canonical fields and the feed tag names that mean them.
ALIASES = {
    "id": ("id", "sku", "stock_number", "stocknumber", "reference", "ref", "item_group_id"),
    "title": ("title", "name", "product_name"),
    "brand": ("brand", "make", "manufacturer"),
    "model": ("model", "model_name"),
    "year": ("year", "model_year", "modelyear"),
    "description": ("description", "summary", "short_description"),
    "url": ("link", "url", "product_url", "deeplink"),
    "image": ("image_link", "image", "image_url", "picture", "photo"),
}
_ALIAS_LOOKUP = {alias: field for field, names in ALIASES.items() for alias in names}

# Tags that are usually the repeating record in a feed, tried first.
LIKELY_ITEM_TAGS = ("item", "product", "entry", "vehicle", "car", "offer", "ad", "listing")


class CatalogueError(RuntimeError):
    """A feed could not be fetched or understood."""


def catalogues_dir() -> Path:
    configured = os.environ.get("CATALOGUES_DIR", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parent / "catalogues"


# -- fetching ----------------------------------------------------------------

def _allow_private() -> bool:
    """Off in production. A local feed server for development needs it on."""
    return os.environ.get("CATALOGUE_ALLOW_PRIVATE_HOSTS", "").strip() in {"1", "true", "yes"}


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CatalogueError("A feed URL must start with http:// or https://.")
    if _allow_private():
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise CatalogueError(f"Could not resolve {parsed.hostname}.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise CatalogueError(f"{parsed.hostname} is not a public address; feeds must be on the internet.")


def fetch_feed(url: str) -> bytes:
    """Download a feed, refusing private hosts on every hop and anything
    larger than MAX_FEED_BYTES."""
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(current)
        try:
            response = requests.get(current, timeout=FETCH_TIMEOUT, stream=True, allow_redirects=False,
                                    headers={"User-Agent": "ANB-AI-Agent-Catalogue/1.0"})
        except requests.RequestException as exc:
            raise CatalogueError(f"Could not download the feed ({exc.__class__.__name__}).") from exc

        if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise CatalogueError("The feed redirected without saying where.")
            current = urljoin(current, location)
            continue

        if not response.ok:
            response.close()
            raise CatalogueError(f"The feed returned HTTP {response.status_code}.")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_FEED_BYTES:
                response.close()
                raise CatalogueError(f"The feed is larger than {MAX_FEED_BYTES // (1024 * 1024)} MB.")
        return bytes(body)

    raise CatalogueError("The feed redirected too many times.")


# -- parsing -----------------------------------------------------------------

def _local(tag: str) -> str:
    """'{http://base.google.com/ns/1.0}price' -> 'price'."""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def is_price_field(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in PRICE_MARKERS)


def mask_money(text: str) -> str:
    return _MONEY.sub("[price on request]", text)


def _leaf_fields(element: ET.Element) -> dict[str, str]:
    """A record's direct leaf children as name -> text (first non-empty wins)."""
    fields: dict[str, str] = {}
    for child in element:
        if len(child):
            continue  # nested groups (shipping, variants) are not item fields
        name = _local(child.tag)
        text = (child.text or "").strip()
        if name and text and name not in fields:
            fields[name] = text
    return fields


def detect_item_tag(root: ET.Element) -> str | None:
    """The repeating record element: the most frequent tag whose elements
    carry at least two leaf fields, preferring the usual names."""
    counts: Counter[str] = Counter()
    for element in root.iter():
        if len(_leaf_fields(element)) >= 2:
            counts[_local(element.tag)] += 1
    if not counts:
        return None
    for tag in LIKELY_ITEM_TAGS:
        if counts.get(tag, 0) >= 1:
            return tag
    return counts.most_common(1)[0][0]


def parse_feed(data: bytes, item_tag: str | None = None) -> dict:
    """Items from a feed, price-free, plus what was detected about it."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CatalogueError(f"The feed is not valid XML ({exc}).") from exc

    tag = (item_tag or "").strip().lower() or detect_item_tag(root)
    if not tag:
        raise CatalogueError("Could not find repeating product records in the feed.")

    items: list[dict] = []
    seen_fields: Counter[str] = Counter()
    dropped: set[str] = set()

    for element in root.iter():
        if _local(element.tag) != tag:
            continue
        raw = _leaf_fields(element)
        if not raw:
            continue

        item: dict = {"attributes": {}}
        for name, text in raw.items():
            if is_price_field(name):
                dropped.add(name)
                continue
            seen_fields[name] += 1
            text = mask_money(text)[:1000]
            canonical = _ALIAS_LOOKUP.get(name)
            if canonical and canonical not in item:
                item[canonical] = text
            elif len(item["attributes"]) < 20:
                item["attributes"][name] = text[:200]

        if not (item.get("title") or item.get("brand") or item.get("model")):
            continue  # nothing an agent could recommend by
        item.setdefault("id", str(len(items) + 1))
        items.append(item)
        if len(items) >= MAX_ITEMS:
            break

    if not items:
        raise CatalogueError(f"Found <{tag}> records, but none had a title, brand or model.")

    return {
        "item_tag": tag,
        "items": items,
        "fields": [name for name, _ in seen_fields.most_common()],
        "dropped_price_fields": sorted(dropped),
    }


# -- snapshots on disk ---------------------------------------------------------

_lock = threading.Lock()


def _snapshot_path(catalogue_id: str | int) -> Path:
    cleaned = re.sub(r"[^0-9a-zA-Z_-]", "", str(catalogue_id)) or "unknown"
    return catalogues_dir() / f"catalogue-{cleaned}.json"


def read_snapshot(catalogue_id: str | int) -> dict | None:
    path = _snapshot_path(catalogue_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_snapshot(catalogue_id: str | int, snapshot: dict) -> None:
    path = _snapshot_path(catalogue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)  # atomic: a reader never sees half a catalogue


def forget(catalogue_id: str | int) -> bool:
    path = _snapshot_path(catalogue_id)
    if path.is_file():
        path.unlink()
        return True
    return False


def sync(catalogue: dict) -> dict:
    """Fetch and store one catalogue now. On failure the last good items are
    kept and the error recorded, so a feed outage never empties the agent's
    catalogue."""
    catalogue_id = catalogue.get("id")
    url = str(catalogue.get("feed_url") or "")
    now = datetime.now(timezone.utc).isoformat()
    previous = read_snapshot(catalogue_id) or {}

    try:
        parsed = parse_feed(fetch_feed(url), catalogue.get("item_element"))
    except CatalogueError as exc:
        snapshot = {**previous, "id": catalogue_id, "name": catalogue.get("name"), "feed_url": url,
                    "last_attempt_at": now, "last_error": str(exc), "last_attempt_epoch": time.time()}
        with _lock:
            _write_snapshot(catalogue_id, snapshot)
        print(f"[catalogue] {catalogue_id} sync failed: {exc}", file=sys.stderr)
        return snapshot

    snapshot = {
        "id": catalogue_id,
        "name": catalogue.get("name"),
        "feed_url": url,
        "item_tag": parsed["item_tag"],
        "fields": parsed["fields"],
        "dropped_price_fields": parsed["dropped_price_fields"],
        "items": parsed["items"],
        "item_count": len(parsed["items"]),
        "synced_at": now,
        "synced_epoch": time.time(),
        "last_attempt_at": now,
        "last_attempt_epoch": time.time(),
        "last_error": None,
    }
    with _lock:
        _write_snapshot(catalogue_id, snapshot)
    print(f"[catalogue] {catalogue_id} synced: {len(parsed['items'])} items", file=sys.stderr)
    return snapshot


def items_for(catalogue: dict) -> list[dict]:
    """A catalogue's items, refreshing first when the snapshot is older than
    the catalogue's refresh interval (and not retrying a failing feed on every
    single search)."""
    snapshot = read_snapshot(catalogue.get("id"))
    refresh = max(5, int(catalogue.get("refresh_minutes") or 60)) * 60
    now = time.time()

    stale = (
        snapshot is None
        or not snapshot.get("items")
        or snapshot.get("feed_url") != catalogue.get("feed_url")
        or now - float(snapshot.get("synced_epoch") or 0) > refresh
    )
    recently_failed = snapshot is not None and snapshot.get("last_error") \
        and now - float(snapshot.get("last_attempt_epoch") or 0) < RETRY_AFTER_FAILURE

    if stale and not recently_failed:
        snapshot = sync(catalogue)

    return (snapshot or {}).get("items") or []


def stats(catalogue_id: str | int) -> dict:
    """What the CRM shows: counts and freshness, never the items."""
    snapshot = read_snapshot(catalogue_id)
    if snapshot is None:
        return {"id": catalogue_id, "synced": False}
    return {
        "id": catalogue_id,
        "synced": bool(snapshot.get("items")),
        "item_count": snapshot.get("item_count", len(snapshot.get("items") or [])),
        "item_tag": snapshot.get("item_tag"),
        "fields": snapshot.get("fields", []),
        "dropped_price_fields": snapshot.get("dropped_price_fields", []),
        "synced_at": snapshot.get("synced_at"),
        "last_attempt_at": snapshot.get("last_attempt_at"),
        "last_error": snapshot.get("last_error"),
    }


# -- searching ---------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def search(catalogues: list[dict], query: str = "", brand: str = "", model: str = "",
           min_year: int | None = None, limit: int = 8) -> dict:
    """Search every catalogue an agent has. Structured filters narrow, the
    free-text query ranks; title, brand and model count double."""
    wanted = _tokens(query)
    results: list[tuple[float, dict]] = []
    unavailable: list[str] = []

    for catalogue in catalogues:
        items = items_for(catalogue)
        if not items:
            unavailable.append(str(catalogue.get("name") or catalogue.get("id")))
            continue
        for item in items:
            if brand and _compact(brand) not in _compact(item.get("brand", "") + " " + item.get("title", "")):
                continue
            if model and _compact(model) not in _compact(item.get("model", "") + " " + item.get("title", "")):
                continue
            if min_year:
                year = re.search(r"\d{4}", str(item.get("year", "")))
                if year and int(year.group()) < min_year:
                    continue

            strong = _tokens(" ".join(str(item.get(k, "")) for k in ("title", "brand", "model")))
            weak = _tokens(" ".join([str(item.get("description", ""))] + list(item.get("attributes", {}).values())))
            score = (2 * len(wanted & strong) + len(wanted & weak)) if wanted else 1.0
            if wanted and score == 0:
                continue
            results.append((score, {**item, "catalogue": catalogue.get("name")}))

    results.sort(key=lambda pair: pair[0], reverse=True)
    return {
        "results": [item for _, item in results[: max(1, min(limit, 20))]],
        "matches_found": len(results),
        "unavailable_catalogues": unavailable,
    }
