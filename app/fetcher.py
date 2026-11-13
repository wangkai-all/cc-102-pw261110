"""Fetching feeds over HTTP or from bundled fixture versions.

Conditional request state (ETag / Last-Modified) lives on the feed row and is
sent back on the next attempt.  A 304 is a successful fetch that changes
nothing.  Failures never raise into the caller's loop: they are returned as a
``FetchOutcome`` so one broken source cannot stop the others.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .feedparse import FeedParseError, ParsedFeed, parse_feed, iso

USER_AGENT = "TopicFeed/1.0 (+local reading tool)"
DEFAULT_TIMEOUT = 10.0

# Retry backoff in seconds.  A paused source is never picked up by the
# scheduler, but manual "retry now" is still allowed and clears the timer.
BACKOFF_STEPS = [30, 120, 600, 1800, 3600, 7200]


@dataclass
class FetchOutcome:
    ok: bool
    status: str  # ok | not_modified | http_error | network_error | parse_error | skipped
    http_status: int | None = None
    not_modified: bool = False
    parsed: ParsedFeed | None = None
    etag: str | None = None
    last_modified: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    source_version: str = ""
    duration_ms: int = 0
    attempts: int = 1
    extra_issues: list[dict[str, str]] = field(default_factory=list)


class FixtureRegistry:
    """Reads bundled fixture manifests.  Version selection happens in the UI."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self._cache: dict[str, dict[str, Any]] = {}

    def _manifest(self, slug: str) -> dict[str, Any] | None:
        if slug in self._cache:
            return self._cache[slug]
        path = self.root / slug / "manifest.json"
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            data = json.load(handle)
        self._cache[slug] = data
        return data

    def slugs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir() and (child / "manifest.json").is_file()
        )

    def describe(self, slug: str) -> dict[str, Any] | None:
        manifest = self._manifest(slug)
        if manifest is None:
            return None
        return {
            "slug": manifest["slug"],
            "title": manifest.get("title", slug),
            "topic": manifest.get("topic", "未分类"),
            "description": manifest.get("description", ""),
            "default_version": manifest.get("default_version"),
            "versions": [
                {
                    "id": v["id"],
                    "label": v.get("label", v["id"]),
                    "status": v.get("status", 200),
                    "notes": v.get("notes", ""),
                }
                for v in manifest.get("versions", [])
            ],
        }

    def all_descriptions(self) -> list[dict[str, Any]]:
        result = []
        for slug in self.slugs():
            description = self.describe(slug)
            if description:
                result.append(description)
        return result

    def version(self, slug: str, version_id: str | None) -> dict[str, Any] | None:
        manifest = self._manifest(slug)
        if manifest is None:
            return None
        wanted = version_id or manifest.get("default_version")
        for version in manifest.get("versions", []):
            if version["id"] == wanted:
                result = dict(version)
                result["slug"] = slug
                result["title"] = manifest.get("title", slug)
                result["topic"] = manifest.get("topic", "未分类")
                return result
        return None

    def read(self, slug: str, version_id: str | None) -> bytes:
        version = self.version(slug, version_id)
        if version is None:
            raise FeedParseError(f"unknown fixture version {version_id!r} for {slug!r}")
        if not version.get("file"):
            raise FeedParseError(f"fixture version {version['id']} has no document")
        path = self.root / slug / version["file"]
        return path.read_bytes()


def fixture_url(slug: str, version: str | None = None) -> str:
    base = f"fixture://{slug}"
    if version:
        return f"{base}?version={version}"
    return base


def fetch_feed(
    feed: dict[str, Any] | Any,
    *,
    fixtures: FixtureRegistry,
    fixture_version: str | None = None,
    force_version: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 1,
    retry_delay: float = 0.0,
    http_get: Any = None,
    force: bool = False,
) -> FetchOutcome:
    """Fetch one feed and parse it.

    ``feed`` may be a sqlite3.Row or a mapping.  Conditional headers are only
    sent for http feeds and only when the caller did not ask to force a
    fresh read (a forced refresh still revalidates, it just never treats a
    cached local state as a reason to skip the request).
    """
    def get(key: str, default: Any = "") -> Any:
        try:
            value = feed[key]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None else value

    kind = get("kind", "http")
    started = time.monotonic()
    attempts = 0
    last_error: FetchOutcome | None = None

    while attempts < max(1, retries):
        attempts += 1
        if kind == "fixture":
            outcome = _fetch_fixture(
                slug=get("fixture_slug"),
                requested_version=fixture_version or get("fixture_version") or None,
                fixtures=fixtures,
                feed=feed,
                get=get,
                force_version=force_version,
                force=force,
            )
        else:
            outcome = _fetch_http(
                url=get("url"),
                etag=get("etag") or None,
                last_modified=get("last_modified") or None,
                timeout=timeout,
                http_get=http_get,
                conditional=not force,
            )
        outcome.attempts = attempts
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if outcome.ok or outcome.status in ("not_modified", "parse_error"):
            return outcome
        if outcome.error_code == "bad_url":
            # Retrying a syntactically invalid URL cannot succeed.
            return outcome
        last_error = outcome
        if attempts < max(1, retries) and retry_delay > 0:
            time.sleep(retry_delay)

    assert last_error is not None
    return last_error


def _fetch_fixture(
    *,
    slug: str,
    requested_version: str | None,
    fixtures: FixtureRegistry,
    feed: Any,
    get: Any,
    force_version: bool,
    force: bool = False,
) -> FetchOutcome:
    if not slug:
        return FetchOutcome(
            ok=False,
            status="network_error",
            error_code="bad_fixture",
            error_detail="feed row has no fixture slug",
        )
    version = fixtures.version(slug, requested_version)
    if version is None:
        return FetchOutcome(
            ok=False,
            status="network_error",
            error_code="bad_fixture",
            error_detail=f"unknown fixture version {requested_version!r}",
        )

    source_version = f"{slug}@{version['id']}"
    status = int(version.get("status", 200))
    if status >= 400:
        return FetchOutcome(
            ok=False,
            status="http_error",
            http_status=status,
            error_code=f"http_{status}",
            error_detail=version.get("notes") or f"fixture simulates HTTP {status}",
            source_version=source_version,
        )

    etag = version.get("etag")
    last_modified = version.get("last_modified")
    # Conditional request: if the stored validators match this exact fixture
    # version, answer 304 like a well-behaved server would.
    if not force and not force_version and etag and etag == (get("etag") or None):
        return FetchOutcome(
            ok=True,
            status="not_modified",
            http_status=304,
            not_modified=True,
            etag=etag,
            last_modified=last_modified,
            source_version=source_version,
        )

    try:
        raw = fixtures.read(slug, version["id"])
    except (OSError, FeedParseError) as exc:
        return FetchOutcome(
            ok=False,
            status="network_error",
            error_code="fixture_read_error",
            error_detail=str(exc),
            source_version=source_version,
        )

    try:
        parsed = parse_feed(raw, base_url=version.get("link") or None)
    except FeedParseError as exc:
        return FetchOutcome(
            ok=False,
            status="parse_error",
            http_status=status,
            error_code="bad_xml",
            error_detail=str(exc),
            etag=etag,
            last_modified=last_modified,
            source_version=source_version,
        )

    return FetchOutcome(
        ok=True,
        status="ok",
        http_status=status,
        parsed=parsed,
        etag=etag,
        last_modified=last_modified,
        source_version=source_version,
    )


def _fetch_http(
    *,
    url: str,
    etag: str | None,
    last_modified: str | None,
    timeout: float,
    http_get: Any = None,
    conditional: bool = True,
) -> FetchOutcome:
    if not url:
        return FetchOutcome(
            ok=False, status="network_error", error_code="bad_url", error_detail="empty URL"
        )
    headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.5"}
    if conditional:
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

    opener = http_get or urllib.request.urlopen
    try:
        # Construction is inside the try on purpose: a URL with a space or a
        # broken IPv6 literal raises before any request is made, and a single
        # bad subscription must surface as an error on that feed rather than
        # escape into the caller's loop.
        request = urllib.request.Request(url, headers=headers, method="GET")
        with opener(request, timeout=timeout) as response:
            raw = response.read()
            return _parse_http_body(
                raw,
                url=url,
                http_status=getattr(response, "status", 200),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchOutcome(
                ok=True,
                status="not_modified",
                http_status=304,
                not_modified=True,
                etag=etag,
                last_modified=last_modified,
            )
        detail = f"HTTP {exc.code} {exc.reason}".strip()
        try:
            body = exc.read(400).decode("utf-8", "replace").strip()
        except Exception:  # pragma: no cover - defensive
            body = ""
        if body:
            detail = f"{detail}: {body}"
        return FetchOutcome(
            ok=False,
            status="http_error",
            http_status=exc.code,
            error_code=f"http_{exc.code}",
            error_detail=detail,
        )
    except (http.client.InvalidURL, ValueError) as exc:
        # A bad port/IPv6 literal or a control character in the URL.  This is
        # a permanent property of the subscription, not a transient outage,
        # so ``fetch_feed`` must not burn retries on it: see the retry loop,
        # which returns ``bad_url`` outcomes immediately.
        return FetchOutcome(
            ok=False,
            status="network_error",
            error_code="bad_url",
            error_detail=f"invalid URL: {exc}",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return FetchOutcome(
            ok=False,
            status="network_error",
            error_code="network_error",
            error_detail=f"request failed: {reason}",
        )


def _parse_http_body(
    raw: bytes,
    *,
    url: str,
    http_status: int,
    etag: str | None,
    last_modified: str | None,
) -> FetchOutcome:
    try:
        parsed = parse_feed(raw, base_url=url)
    except FeedParseError as exc:
        return FetchOutcome(
            ok=False,
            status="parse_error",
            http_status=http_status,
            error_code="bad_xml",
            error_detail=str(exc),
            etag=etag,
            last_modified=last_modified,
        )
    return FetchOutcome(
        ok=True,
        status="ok",
        http_status=http_status,
        parsed=parsed,
        etag=etag,
        last_modified=last_modified,
    )


def next_retry_delay(failure_count: int) -> int:
    index = min(max(failure_count - 1, 0), len(BACKOFF_STEPS) - 1)
    base = BACKOFF_STEPS[index]
    # Jitter keeps a batch of failing feeds from retrying in lockstep.
    return int(base * random.uniform(0.9, 1.1))


def retry_at(failure_count: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return iso(now + timedelta(seconds=next_retry_delay(failure_count))) or ""
