"""Refresh orchestration: per-source isolation, conditional requests, retries."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from . import store
from .feedparse import iso
from .fetcher import FetchOutcome, FixtureRegistry, fetch_feed, retry_at
from .fetcher import fixture_url


def _apply_success(
    conn: sqlite3.Connection,
    feed_id: int,
    outcome: FetchOutcome,
    *,
    started: datetime,
    finished: datetime,
) -> dict[str, Any]:
    if outcome.not_modified:
        fetch_id = store.record_fetch(
            conn,
            feed_id,
            started_at=started,
            finished_at=finished,
            status=outcome.status,
            http_status=304,
            not_modified=True,
            etag=outcome.etag,
            source_version=outcome.source_version,
        )
        store.update_feed_fields(
            conn,
            feed_id,
            last_fetch_at=iso(finished),
            last_http_status=304,
            last_result="not_modified",
            last_error_code=None,
            last_error_detail=None,
            failure_count=0,
            next_retry_at=None,
            etag=outcome.etag or None,
            last_modified=outcome.last_modified or None,
        )
        # A 304 can only happen against an ETag/Last-Modified that we stored
        # after a successful parse, so the source is reachable and the
        # document we hold is still current: any earlier source-level failure
        # (404/500/timeout/bad XML) has demonstrably cleared.
        store.resolve_feed_issues(conn, feed_id)
        return {
            "feed_id": feed_id,
            "status": "not_modified",
            "http_status": 304,
            "stats": {"seen": 0, "new": 0, "changed": 0, "missing": 0},
            "fetch_id": fetch_id,
            "source_version": outcome.source_version,
        }

    assert outcome.parsed is not None
    # Log the attempt before writing articles, but mark it "ingesting" so a
    # crash mid-write leaves an honest incomplete record rather than a
    # success claim.  It is promoted to "ok" only after ingestion commits.
    fetch_id = store.record_fetch(
        conn,
        feed_id,
        started_at=started,
        finished_at=finished,
        status="ingesting",
        http_status=outcome.http_status,
        etag=outcome.etag,
        source_version=outcome.source_version,
    )
    try:
        stats = store.ingest(
            conn,
            feed_id,
            outcome.parsed,
            fetch_id=fetch_id,
            source_version=outcome.source_version,
            observed_at=finished,
        )
    except Exception:
        store.update_fetch(conn, fetch_id, status="failed", error_code="ingest_error")
        raise
    store.update_fetch(conn, fetch_id, status=outcome.status, stats=stats)
    store.update_feed_fields(
        conn,
        feed_id,
        title=outcome.parsed.title or (store.get_feed(conn, feed_id)["title"] or ""),
        last_fetch_at=iso(finished),
        last_success_at=iso(finished),
        last_http_status=outcome.http_status,
        last_result="ok",
        last_error_code=None,
        last_error_detail=None,
        failure_count=0,
        next_retry_at=None,
        etag=outcome.etag or None,
        last_modified=outcome.last_modified or None,
    )
    store.resolve_feed_issues(conn, feed_id)
    return {
        "feed_id": feed_id,
        "status": "ok",
        "http_status": outcome.http_status,
        "stats": stats,
        "fetch_id": fetch_id,
        "source_version": outcome.source_version,
        "title": outcome.parsed.title,
    }


def _apply_failure(
    conn: sqlite3.Connection,
    feed_id: int,
    outcome: FetchOutcome,
    *,
    started: datetime,
    finished: datetime,
    failure_count: int,
) -> dict[str, Any]:
    fetch_id = store.record_fetch(
        conn,
        feed_id,
        started_at=started,
        finished_at=finished,
        status=outcome.status,
        http_status=outcome.http_status,
        error_code=outcome.error_code,
        error_detail=outcome.error_detail,
        etag=outcome.etag,
        source_version=outcome.source_version,
    )
    next_at = retry_at(failure_count, finished)
    store.update_feed_fields(
        conn,
        feed_id,
        last_fetch_at=iso(finished),
        last_http_status=outcome.http_status,
        last_result="failed" if outcome.status != "parse_error" else "parse_error",
        last_error_code=outcome.error_code or "unknown_error",
        last_error_detail=outcome.error_detail or "",
        failure_count=failure_count,
        next_retry_at=next_at,
    )
    kind = "bad_xml" if outcome.status == "parse_error" else outcome.error_code or "network_error"
    store.record_feed_issue(
        conn,
        feed_id,
        kind=kind,
        detail=outcome.error_detail or "unknown failure",
        stamp=iso(finished),
    )
    return {
        "feed_id": feed_id,
        "status": outcome.status,
        "http_status": outcome.http_status,
        "error_code": outcome.error_code,
        "error_detail": outcome.error_detail,
        "next_retry_at": next_at,
        "fetch_id": fetch_id,
        "source_version": outcome.source_version,
    }


def refresh_feed(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    fixtures: FixtureRegistry,
    force: bool = False,
    version: str | None = None,
    retries: int = 1,
    http_get: Any = None,
    respect_pause: bool = False,
) -> dict[str, Any]:
    """Fetch, parse and persist one feed.  Never raises for feed-level problems."""
    feed = store.get_feed(conn, feed_id)
    if feed is None:
        return {"feed_id": feed_id, "status": "missing", "error_code": "no_such_feed"}

    if respect_pause and feed["paused"]:
        return {"feed_id": feed_id, "status": "paused", "reason": "source is paused"}

    # For fixture sources the selected version is the version to request.  A
    # version passed explicitly is a one-off override that is also persisted.
    version_to_use = version or feed["fixture_version"]
    if feed["kind"] == "fixture" and version and version != feed["fixture_version"]:
        store.update_feed_fields(conn, feed_id, fixture_version=version)
        feed = store.get_feed(conn, feed_id)
        version_to_use = version

    started = datetime.now(timezone.utc)
    outcome: FetchOutcome = fetch_feed(
        feed,
        fixtures=fixtures,
        fixture_version=version_to_use,
        force_version=bool(version),
        retries=retries,
        http_get=http_get,
        force=force,
    )
    finished = datetime.now(timezone.utc)

    if outcome.ok:
        return _apply_success(conn, feed_id, outcome, started=started, finished=finished)

    failure_count = int(feed["failure_count"] or 0) + 1
    return _apply_failure(
        conn,
        feed_id,
        outcome,
        started=started,
        finished=finished,
        failure_count=failure_count,
    )


def refresh_all(
    conn: sqlite3.Connection,
    *,
    fixtures: FixtureRegistry,
    force: bool = False,
    respect_pause: bool = True,
    only_due: bool = True,
    now: datetime | None = None,
    http_get: Any = None,
) -> list[dict[str, Any]]:
    """Refresh every feed independently.

    A crash or exception in one feed is contained: it becomes that feed's
    result row and the loop continues.
    """
    now = now or datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    for feed in store.list_feeds(conn):
        if respect_pause and feed["paused"]:
            results.append({"feed_id": feed["id"], "status": "paused"})
            continue
        if not force and only_due and feed["next_retry_at"]:
            try:
                due = datetime.fromisoformat(feed["next_retry_at"].replace("Z", "+00:00"))
            except ValueError:
                due = None
            if due and due > now:
                results.append(
                    {
                        "feed_id": feed["id"],
                        "status": "retry_scheduled",
                        "next_retry_at": feed["next_retry_at"],
                    }
                )
                continue
        try:
            results.append(
                refresh_feed(conn, feed["id"], fixtures=fixtures, force=force, http_get=http_get)
            )
        except Exception as exc:  # keep the batch alive
            stamp = iso(now) or ""
            store.update_feed_fields(
                conn,
                feed["id"],
                last_fetch_at=stamp,
                last_result="failed",
                last_error_code="internal_error",
                last_error_detail=f"{type(exc).__name__}: {exc}",
                failure_count=int(feed["failure_count"] or 0) + 1,
                next_retry_at=retry_at(int(feed["failure_count"] or 0) + 1, now),
            )
            results.append(
                {
                    "feed_id": feed["id"],
                    "status": "internal_error",
                    "error_code": "internal_error",
                    "error_detail": f"{type(exc).__name__}: {exc}",
                }
            )
    return results


def retry_feed(conn: sqlite3.Connection, feed_id: int) -> None:
    """Clear the backoff timer so a manual retry is immediate."""
    store.update_feed_fields(conn, feed_id, next_retry_at=None)


def set_paused(conn: sqlite3.Connection, feed_id: int, paused: bool) -> None:
    store.update_feed_fields(conn, feed_id, paused=1 if paused else 0)


def set_fixture_version(conn: sqlite3.Connection, feed_id: int, version: str) -> dict[str, Any]:
    feed = store.get_feed(conn, feed_id)
    if feed is None:
        return {"status": "missing"}
    if feed["kind"] != "fixture":
        return {"status": "invalid", "detail": "not a fixture source"}
    store.update_feed_fields(conn, feed_id, fixture_version=version)
    return {"status": "ok", "fixture_version": version}


def seed_fixture_feeds(conn: sqlite3.Connection, fixtures: FixtureRegistry) -> list[int]:
    """Register the bundled demo sources once, keeping any version the user chose."""
    created: list[int] = []
    for description in fixtures.all_descriptions():
        slug = description["slug"]
        default_version = description["default_version"]
        existing = store.get_feed_by_url(conn, fixture_url(slug, default_version))
        if existing is not None:
            # Never overwrite the version the user picked in the UI.
            continue
        # A feed row added earlier with an explicit version uses that URL;
        # recognise it by slug so we do not register the same source twice.
        already = conn.execute(
            "SELECT 1 FROM feeds WHERE kind = 'fixture' AND fixture_slug = ?", (slug,)
        ).fetchone()
        if already is not None:
            continue
        created.append(
            store.create_feed(
                conn,
                url=fixture_url(slug, default_version),
                kind="fixture",
                fixture_slug=slug,
                fixture_version=default_version,
                topic=description["topic"],
                title=description["title"],
            )
        )
    return created


class Scheduler:
    """Background loop that refreshes due sources.

    Only sources whose backoff timer has expired are fetched, so a failing
    source is not hammered.  Paused sources are never touched here — a manual
    refresh is still possible from the UI.  Every iteration opens its own
    connection: SQLite connections are not shareable across threads.
    """

    def __init__(
        self,
        app: Any,
        *,
        interval: float = 20.0,
        fixtures: FixtureRegistry | None = None,
    ) -> None:
        self.app = app
        self.fixtures = fixtures or app.fixtures
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: str | None = None
        self.last_results: list[dict[str, Any]] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="feed-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def run_once(self) -> list[dict[str, Any]]:
        conn = self.app.connect()
        try:
            results = refresh_all(
                conn,
                fixtures=self.fixtures,
                respect_pause=True,
                only_due=True,
            )
        finally:
            conn.close()
        self.last_run = store.now_iso()
        self.last_results = results
        return results

    def _loop(self) -> None:
        # Small initial delay so the server is accepting requests before the
        # first round of network I/O happens.
        if self._stop.wait(min(3.0, self.interval)):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # the scheduler must survive anything
                import traceback

                traceback.print_exc()
            if self._stop.wait(self.interval):
                break
