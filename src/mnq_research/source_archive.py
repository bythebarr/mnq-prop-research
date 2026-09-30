"""Immutable archiving of external sources (Round 16A): the Tradeify commission page first.

An archive stores the RAW response bytes exactly as received, named by their
SHA-256, with a manifest recording the URL, page title, retrieval time (UTC),
any exposed effective/publication date, the relevant extracted content and
the arithmetic check 1.82 / 2 = 0.91.

The archive never edits the configuration. If the archived page no longer
states the configured MNQ round-trip cost, the result is
MISMATCH_REQUIRES_NEW_DECISION: a changed source needs a new owner decision
and a new specification version.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable

TRADEIFY_COMMISSION_URL = "https://help.tradeify.co/en/articles/10468315-trading-commission-fees"


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    body: bytes
    content_type: str


@dataclass(frozen=True)
class ArchiveManifest:
    url: str
    page_title: str | None
    retrieved_utc: str
    effective_or_publication_date: str | None
    raw_sha256: str
    raw_path: str
    relevant_extract: list[str]
    configured_round_trip_usd: str
    configured_per_side_usd: str
    found_round_trip_usd: str | None
    calculation: str
    status: str  # MATCHES_CONFIGURATION | MISMATCH_REQUIRES_NEW_DECISION | FETCH_FAILED


def fetch_with_urllib(url: str) -> FetchedPage:
    """Plain HTTPS GET through the environment's proxy settings (no credentials involved)."""
    import urllib.request

    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "mnq-prop-research-archiver"}), timeout=30) as r:
        return FetchedPage(url, r.status, r.read(), r.headers.get("Content-Type", ""))


def _text(body: bytes) -> str:
    raw = body.decode("utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", raw)))


def archive_commission_source(
    configured_round_trip_usd: str,
    configured_per_side_usd: str,
    out_dir: Path,
    fetch: Callable[[str], FetchedPage] = fetch_with_urllib,
    url: str = TRADEIFY_COMMISSION_URL,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
) -> ArchiveManifest:
    rt, side = Decimal(configured_round_trip_usd), Decimal(configured_per_side_usd)
    calculation = f"{rt} / 2 = {rt / 2} (configured per side {side}: {'consistent' if rt / 2 == side else 'INCONSISTENT'})"
    retrieved = now().isoformat()
    try:
        page = fetch(url)
        if page.status != 200 or not page.body:
            raise RuntimeError(f"HTTP {page.status}")
    except Exception as exc:  # noqa: BLE001
        return ArchiveManifest(url, None, retrieved, None, "", "", [f"FETCH_FAILED: {type(exc).__name__}: {exc}"],
                               str(rt), str(side), None, calculation, "FETCH_FAILED")
    digest = hashlib.sha256(page.body).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"{digest}.html"
    raw_path.write_bytes(page.body)  # the exact bytes received
    body = page.body.decode("utf-8", errors="replace")
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    title = html.unescape(title_match.group(1)).strip() if title_match else None
    date_match = re.search(r'(?i)"(?:dateModified|datePublished|article:modified_time)"\s*:?\s*"?([0-9]{4}-[0-9]{2}-[0-9]{2}[^"]*)', body)
    text = _text(page.body)
    extract, found = [], None
    for m in re.finditer(r"MNQ|Micro E-?Mini NASDAQ", text):
        extract.append(text[max(m.start() - 80, 0): m.start() + 200].strip())
        amount = re.search(r"\$\s?([0-9]+\.[0-9]{2})", text[m.start(): m.start() + 200])  # first amount after the mention
        if found is None and amount:
            found = amount.group(1)
    status = "MATCHES_CONFIGURATION" if found is not None and Decimal(found) == rt and rt / 2 == side else "MISMATCH_REQUIRES_NEW_DECISION"
    manifest = ArchiveManifest(url, title, retrieved, date_match.group(1) if date_match else None, digest, str(raw_path),
                               extract, str(rt), str(side), found, calculation, status)
    (out_dir / f"{digest}.manifest.json").write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True))
    return manifest


def capture_raw_source(url: str, out_dir: Path, fetch: Callable[[str], FetchedPage] = fetch_with_urllib,
                       now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> dict:
    """Archive one official page exactly as received (raw bytes named by SHA-256). No parsing, no interpretation."""
    retrieved = now().isoformat()
    try:
        page = fetch(url)
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "retrieved_utc": retrieved, "status": "FETCH_FAILED", "error": f"{type(exc).__name__}: {exc}"}
    if page.status != 200 or not page.body:
        return {"url": url, "retrieved_utc": retrieved, "status": "FETCH_FAILED", "error": f"HTTP {page.status}"}
    digest = hashlib.sha256(page.body).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"{digest}.html"
    raw_path.write_bytes(page.body)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page.body.decode("utf-8", errors="replace"))
    manifest = {"url": url, "retrieved_utc": retrieved, "status": "CAPTURED", "http_status": page.status,
                "content_type": page.content_type, "bytes": len(page.body), "raw_sha256": digest, "raw_path": str(raw_path),
                "page_title": html.unescape(title.group(1)).strip() if title else None,
                "parsed": False, "note": "raw capture only; not parsed, not a READY calendar"}
    (out_dir / f"{digest}.manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest
