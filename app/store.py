"""SQLite storage, identity resolution, revision tracking and export.

Design notes
------------
* Every entry belongs to exactly one feed.  Its identity is, in order of
  preference: the feed's stable id (``guid``/``id``), the normalised link,
  and finally a synthetic content fingerprint.  Title alone is never used.
* A revision row is appended only when the observable state (title + body)
  changes.  Re-ingesting identical content is a no-op, so repeated updates
  do not grow either the entry list or the revision history.
* Entries that disappear from a feed are never deleted.  They are marked as
  unseen for the current round, which is what makes "feed only returns the
  latest few" safe.
* ``published_at`` comes from the feed and is never replaced by our fetch
  time.  ``first_seen_at`` is our own record and is displayed separately so
  a re-published old article cannot masquerade as a new one.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .feedparse import ParsedEntry, ParsedFeed, iso, strip_html

REVISION_SEPARATOR = "\n\n---\u0000---\n\n"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return iso(utcnow()) or ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT UNIQUE NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'http',
    fixture_slug      TEXT,
    fixture_version   TEXT,
    title             TEXT NOT NULL DEFAULT '',
    site_link         TEXT NOT NULL DEFAULT '',
    topic             TEXT NOT NULL DEFAULT '未分类',
    paused            INTEGER NOT NULL DEFAULT 0,
    etag              TEXT,
    last_modified     TEXT,
    last_fetch_at     TEXT,
    last_success_at   TEXT,
    last_http_status  INTEGER,
    last_result       TEXT NOT NULL DEFAULT 'never',
    last_error_code   TEXT,
    last_error_detail TEXT,
    failure_count     INTEGER NOT NULL DEFAULT 0,
    next_retry_at     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id           INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    identity_key      TEXT NOT NULL,
    identity_kind     TEXT NOT NULL,
    guid              TEXT NOT NULL DEFAULT '',
    link              TEXT NOT NULL DEFAULT '',
    link_norm         TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    published_at      TEXT,
    updated_at        TEXT,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    last_seen_fetch_id INTEGER,
    missing_rounds    INTEGER NOT NULL DEFAULT 0,
    content_hash      TEXT NOT NULL DEFAULT '',
    content_html      TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    UNIQUE(feed_id, identity_key)
);

CREATE TABLE IF NOT EXISTS entry_revisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id       INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    revision_no    INTEGER NOT NULL,
    signature      TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    content_html   TEXT NOT NULL DEFAULT '',
    published_at   TEXT,
    updated_at     TEXT,
    observed_at    TEXT NOT NULL,
    fetch_id       INTEGER,
    source_version TEXT NOT NULL DEFAULT '',
    UNIQUE(entry_id, revision_no)
);

CREATE TABLE IF NOT EXISTS entry_links (
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    link          TEXT NOT NULL,
    link_norm     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY(entry_id, link_norm)
);

CREATE TABLE IF NOT EXISTS feed_fetches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id        INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    http_status    INTEGER,
    not_modified   INTEGER NOT NULL DEFAULT 0,
    entries_seen   INTEGER NOT NULL DEFAULT 0,
    entries_new    INTEGER NOT NULL DEFAULT 0,
    entries_changed INTEGER NOT NULL DEFAULT 0,
    entries_missing INTEGER NOT NULL DEFAULT 0,
    error_code     TEXT,
    error_detail   TEXT,
    duration_ms    INTEGER,
    etag           TEXT,
    source_version TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS entry_issues (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id        INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    entry_id       INTEGER REFERENCES entries(id) ON DELETE CASCADE,
    identity_key   TEXT NOT NULL DEFAULT '',
    entry_title    TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL,
    field          TEXT NOT NULL DEFAULT '',
    detail         TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    occurrences    INTEGER NOT NULL DEFAULT 1,
    resolved_at    TEXT,
    UNIQUE(feed_id, identity_key, kind, field)
);

CREATE INDEX IF NOT EXISTS idx_entries_feed ON entries(feed_id);
CREATE INDEX IF NOT EXISTS idx_entries_published ON entries(published_at);
CREATE INDEX IF NOT EXISTS idx_revisions_entry ON entry_revisions(entry_id);
CREATE INDEX IF NOT EXISTS idx_fetches_feed ON feed_fetches(feed_id, id);
CREATE INDEX IF NOT EXISTS idx_issues_feed ON entry_issues(feed_id, resolved_at);
"""


# ---------------------------------------------------------------------------
# Connection helpers


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Identity


def identity_for(entry: ParsedEntry) -> tuple[str, str]:
    """Return ``(key, kind)``.

    Stable in-feed identifier wins.  A link is only used when the feed
    supplies no identifier.  With neither we derive a fingerprint from the
    title *and* body *and* publication date: two different articles that
    merely share a title therefore stay separate.
    """
    guid = (entry.guid or "").strip()
    if guid:
        return "guid:" + guid, "guid"
    if entry.link:
        return "link:" + entry.link, "link"
    # A "published" date that was really lifted from <updated> moves every
    # time the article is edited.  Including it would give the same article a
    # new fingerprint on each edit, i.e. a duplicate article, so that case
    # falls back to title + body only.
    published_material = (
        "" if entry.published_is_fallback else (iso(entry.published) or "")
    )
    body_text = strip_html(entry.content_html)
    if not body_text and not published_material:
        # A body made only of markup (e.g. a single <img>) is still evidence:
        # two such entries with different images are different articles.
        body_text = entry.content_html.strip()
        if not body_text:
            # Only a title remains.  A title-only fingerprint would merge
            # every identically-titled entry that has no id, link, body or
            # date — exactly the "merge by title" the rules forbid.  Such an
            # entry is reported instead of being stored under a guess.
            return "", "none"
    material = "\n".join([entry.title.strip(), published_material, body_text])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "content:" + digest, "content"


def _fallback_lookups(entry: ParsedEntry, primary_kind: str) -> list[tuple[str, set[str]]]:
    """Earlier identity keys this article may already be stored under.

    Each candidate comes with the identity kinds it is allowed to match, so a
    later round can adopt a stronger identifier without inventing a duplicate
    article — and without collapsing two genuinely separate ones.

    * Link fallback: safe when the previous row was identified by the same
      normalised link, or by a guid *and* the same permalink.  A source that
      drops its guid while keeping the permalink is still the same post.
    * Content-fingerprint fallback: only for rows that previously had neither
      an id nor a link.  A fingerprint includes title, publication date and
      body, so matching it never merges articles on title alone.
    """
    candidates: list[tuple[str, set[str]]] = []
    if primary_kind in ("guid", "link") and entry.link:
        candidates.append(("link:" + entry.link, {"link", "guid"}))
    if primary_kind in ("guid", "link"):
        fingerprint_key, _ = identity_for(
            ParsedEntry(
                title=entry.title,
                link="",
                guid="",
                published=entry.published,
                updated=entry.updated,
                content_html=entry.content_html,
                summary=entry.summary,
                published_is_fallback=entry.published_is_fallback,
            )
        )
        candidates.append((fingerprint_key, {"content"}))
    return candidates


def _find_by_link(conn: sqlite3.Connection, feed_id: int, link: str) -> list[sqlite3.Row]:
    """All entries previously seen at this normalised link.

    The link history table is the authority here: it also covers guid rows,
    whose identity key is not the link.  Several articles may share one
    permalink, so the caller must consider every row and apply its own guard
    rails rather than trusting the first one.
    """
    return conn.execute(
        """SELECT DISTINCT e.* FROM entry_links l
           JOIN entries e ON e.id = l.entry_id
           WHERE e.feed_id = ? AND l.link_norm = ?
           ORDER BY e.id""",
        (feed_id, link),
    ).fetchall()


def _match_weaker_identity(
    conn: sqlite3.Connection, feed_id: int, entry: ParsedEntry, primary_kind: str
) -> sqlite3.Row | None:
    """Look for this article under an identity the feed used in a past round.

    The guard rails matter more than the convenience: adopting a weaker
    identifier must never collapse two different articles.

    * A row that already carries a *different* guid is never matched.
    * When the incoming entry has no guid of its own, a link match against a
      guid row additionally requires the same title and the same (or an
      unknown) publication date, so a repurposed permalink does not swallow
      an unrelated post.
    * Symmetrically, promoting a link/content row to a guid requires a
      compatible publication date.  Two genuinely different posts can share
      a permalink, and the newcomer arriving with a guid must not overwrite
      the older article's history.  The date is the only evidence available
      here: a title or body change is exactly what a normal edit looks like.
    """
    incoming_guid = (entry.guid or "").strip()

    def same_publication(row: sqlite3.Row) -> bool:
        incoming_pub = iso(entry.published)
        return (
            incoming_pub is None
            or row["published_at"] is None
            or row["published_at"] == incoming_pub
        )

    def acceptable(row: sqlite3.Row) -> bool:
        row_guid = (row["guid"] or "").strip()
        if incoming_guid and row_guid and row_guid != incoming_guid:
            return False
        if not incoming_guid and row["identity_kind"] == "guid":
            same_title = (row["title"] or "").strip() == (entry.title or "").strip()
            return same_title and same_publication(row)
        if incoming_guid and not row_guid and row["identity_kind"] in ("link", "content"):
            return same_publication(row)
        return True

    for candidate_key, allowed_kinds in _fallback_lookups(entry, primary_kind):
        if not candidate_key:
            continue
        row = conn.execute(
            "SELECT * FROM entries WHERE feed_id = ? AND identity_key = ?",
            (feed_id, candidate_key),
        ).fetchone()
        if row is not None and row["identity_kind"] in allowed_kinds and acceptable(row):
            return row
    if primary_kind in ("guid", "link") and entry.link:
        for row in _find_by_link(conn, feed_id, entry.link):
            if row["identity_kind"] in ("link", "guid") and acceptable(row):
                return row
    return None


def revision_signature(title: str, content_html: str) -> str:
    material = (title or "").strip() + REVISION_SEPARATOR + strip_html(content_html or "")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def content_hash(content_html: str) -> str:
    return hashlib.sha256(strip_html(content_html or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Feeds


def create_feed(
    conn: sqlite3.Connection,
    *,
    url: str,
    kind: str = "http",
    fixture_slug: str | None = None,
    fixture_version: str | None = None,
    topic: str = "未分类",
    title: str = "",
) -> int:
    stamp = now_iso()
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO feeds (url, kind, fixture_slug, fixture_version, title, topic,
                                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (url, kind, fixture_slug, fixture_version, title, topic or "未分类", stamp, stamp),
        )
    return int(cur.lastrowid)


def get_feed(conn: sqlite3.Connection, feed_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()


def get_feed_by_url(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()


def list_feeds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM feeds ORDER BY topic, title, id").fetchall()


def update_feed_fields(conn: sqlite3.Connection, feed_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "title", "site_link", "topic", "paused", "etag", "last_modified",
        "last_fetch_at", "last_success_at", "last_http_status", "last_result",
        "last_error_code", "last_error_detail", "failure_count", "next_retry_at",
        "fixture_version",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown feed fields: {sorted(unknown)}")
    fields = dict(fields)
    fields["updated_at"] = now_iso()
    columns = ", ".join(f"{key} = ?" for key in fields)
    with transaction(conn):
        conn.execute(f"UPDATE feeds SET {columns} WHERE id = ?", (*fields.values(), feed_id))


def delete_feed(conn: sqlite3.Connection, feed_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))


def topic_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT f.topic AS topic,
                  COUNT(e.id) AS entries,
                  COUNT(DISTINCT f.id) AS feeds,
                  COUNT(DISTINCT CASE WHEN f.paused = 1 THEN f.id END) AS paused_feeds
           FROM feeds f LEFT JOIN entries e ON e.feed_id = f.id
           GROUP BY f.topic
           ORDER BY f.topic"""
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Ingestion


def _latest_revision(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM entry_revisions WHERE entry_id = ? ORDER BY revision_no DESC LIMIT 1",
        (entry_id,),
    ).fetchone()


def _record_link(
    conn: sqlite3.Connection, entry_id: int, link: str, link_norm: str, stamp: str
) -> None:
    if not link:
        return
    conn.execute(
        """INSERT INTO entry_links (entry_id, link, link_norm, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(entry_id, link_norm) DO UPDATE SET
               last_seen_at = excluded.last_seen_at,
               link = excluded.link""",
        (entry_id, link, link_norm, stamp, stamp),
    )


def _record_issue(
    conn: sqlite3.Connection,
    *,
    feed_id: int,
    entry_id: int | None,
    identity_key: str,
    title: str,
    kind: str,
    field: str,
    detail: str,
    stamp: str,
    observed: set[tuple[str, str, str]],
) -> None:
    observed.add((identity_key, kind, field))
    conn.execute(
        """INSERT INTO entry_issues
               (feed_id, entry_id, identity_key, entry_title, kind, field, detail,
                first_seen_at, last_seen_at, occurrences)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
           ON CONFLICT(feed_id, identity_key, kind, field) DO UPDATE SET
               entry_id = excluded.entry_id,
               entry_title = excluded.entry_title,
               detail = excluded.detail,
               last_seen_at = excluded.last_seen_at,
               occurrences = entry_issues.occurrences + 1,
               resolved_at = NULL""",
        (feed_id, entry_id, identity_key, title, kind, field, detail, stamp, stamp),
    )


def ingest(
    conn: sqlite3.Connection,
    feed_id: int,
    parsed: ParsedFeed,
    *,
    fetch_id: int | None = None,
    source_version: str = "",
    observed_at: datetime | None = None,
) -> dict[str, int]:
    """Apply a successfully parsed feed.

    Returns counters used for the fetch log and the UI.
    """
    stamp_dt = observed_at or utcnow()
    stamp = iso(stamp_dt) or ""
    stats = {"seen": 0, "new": 0, "changed": 0, "missing": 0}
    seen_identities: set[str] = set()
    observed_issues: set[tuple[str, str, str]] = set()

    with transaction(conn):
        for parsed_entry in parsed.entries:
            stats["seen"] += 1
            key, kind = identity_for(parsed_entry)
            if kind == "none":
                # Nothing distinguishes this entry from any other one with
                # the same title.  Storing it would merge unrelated posts, and
                # storing it under a fresh key each round would grow the list
                # forever, so report it and move on.  The issue key is never
                # used as an article identity; it only keeps the report
                # distinct per title.
                stats["unidentified"] = stats.get("unidentified", 0) + 1
                report_key = "unidentified:" + hashlib.sha256(
                    (parsed_entry.title + "\u0000" + parsed_entry.content_html).encode("utf-8")
                ).hexdigest()[:16]
                _record_issue(
                    conn,
                    feed_id=feed_id,
                    entry_id=None,
                    identity_key=report_key,
                    title=parsed_entry.title,
                    kind="missing_identity",
                    field="",
                    detail=(
                        "entry has no id, link, body or date, so it cannot be "
                        "told apart from other entries with the same title"
                    ),
                    stamp=stamp,
                    observed=observed_issues,
                )
                continue
            existing = conn.execute(
                "SELECT * FROM entries WHERE feed_id = ? AND identity_key = ?",
                (feed_id, key),
            ).fetchone()

            # A source may start (or stop) supplying a stable id.  Match on
            # the weaker identities this article may already be stored under
            # so its revision history follows it instead of forking.
            if existing is None:
                existing = _match_weaker_identity(conn, feed_id, parsed_entry, kind)

            signature = revision_signature(parsed_entry.title, parsed_entry.content_html)
            final_key, final_kind = key, kind

            if existing is None:
                cur = conn.execute(
                    """INSERT INTO entries
                           (feed_id, identity_key, identity_kind, guid, link, link_norm,
                            title, published_at, updated_at, first_seen_at, last_seen_at,
                            last_seen_fetch_id, missing_rounds, content_hash, content_html,
                            summary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                    (
                        feed_id, key, kind, parsed_entry.guid, parsed_entry.link,
                        parsed_entry.link, parsed_entry.title,
                        iso(parsed_entry.published), iso(parsed_entry.updated),
                        stamp, stamp, fetch_id, content_hash(parsed_entry.content_html),
                        parsed_entry.content_html, parsed_entry.summary,
                    ),
                )
                entry_id = int(cur.lastrowid)
                conn.execute(
                    """INSERT INTO entry_revisions
                           (entry_id, revision_no, signature, title, content_html,
                            published_at, updated_at, observed_at, fetch_id, source_version)
                       VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id, signature, parsed_entry.title,
                        parsed_entry.content_html, iso(parsed_entry.published),
                        iso(parsed_entry.updated), stamp, fetch_id, source_version,
                    ),
                )
                _record_link(conn, entry_id, parsed_entry.link, parsed_entry.link, stamp)
                stats["new"] += 1
            else:
                entry_id = int(existing["id"])
                latest = _latest_revision(conn, entry_id)

                # Publication time is feed data.  Never substitute the time
                # we happen to have fetched it.  A missing value in this
                # round must not erase a previously known one.
                published = iso(parsed_entry.published) or existing["published_at"]
                if parsed_entry.published_is_fallback and existing["published_at"]:
                    # "Published" is really the source's <updated> because the
                    # feed never supplied a publication date.  <updated> moves
                    # with every edit, so adopting the new value would make a
                    # months-old article look freshly published.  The first
                    # value we saw is the closest thing to a publish date.
                    published = existing["published_at"]
                updated = iso(parsed_entry.updated) or existing["updated_at"]
                new_title = parsed_entry.title or existing["title"]
                # A round that omits the title, or ships an empty body, is
                # describing the same article less completely — not changing
                # it.  Keeping what we already have avoids a bogus revision
                # (and a second one when the field comes back).
                new_content = parsed_entry.content_html
                if not strip_html(new_content) and strip_html(existing["content_html"] or ""):
                    new_content = existing["content_html"]
                effective_signature = revision_signature(new_title, new_content)
                changed = latest is None or latest["signature"] != effective_signature
                guid = parsed_entry.guid or existing["guid"]
                link = parsed_entry.link or existing["link"]
                identity_kind = existing["identity_kind"]
                identity_key = existing["identity_key"]
                if identity_kind in ("link", "content"):
                    # Adopt the strongest identifier the feed now supplies.
                    if guid:
                        new_key, new_kind = "guid:" + guid.strip(), "guid"
                    elif parsed_entry.link:
                        new_key, new_kind = "link:" + parsed_entry.link, "link"
                    else:
                        new_key, new_kind = identity_key, identity_kind
                    if new_key != identity_key:
                        clash = conn.execute(
                            "SELECT id FROM entries WHERE feed_id = ? AND identity_key = ? AND id != ?",
                            (feed_id, new_key, entry_id),
                        ).fetchone()
                        if clash is None:
                            identity_key, identity_kind = new_key, new_kind

                conn.execute(
                    """UPDATE entries SET
                           identity_key = ?, identity_kind = ?, guid = ?, link = ?,
                           link_norm = ?, title = ?, published_at = ?, updated_at = ?,
                           last_seen_at = ?, last_seen_fetch_id = ?, missing_rounds = 0,
                           content_hash = ?, content_html = ?, summary = ?
                       WHERE id = ?""",
                    (
                        identity_key, identity_kind, guid, link, link, new_title,
                        published, updated, stamp, fetch_id,
                        content_hash(new_content),
                        new_content, parsed_entry.summary or existing["summary"],
                        entry_id,
                    ),
                )
                _record_link(conn, entry_id, parsed_entry.link, parsed_entry.link, stamp)

                if changed:
                    next_no = int(latest["revision_no"]) + 1 if latest else 1
                    conn.execute(
                        """INSERT INTO entry_revisions
                               (entry_id, revision_no, signature, title, content_html,
                                published_at, updated_at, observed_at, fetch_id,
                                source_version)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            entry_id, next_no, effective_signature, new_title,
                            new_content, published, updated, stamp,
                            fetch_id, source_version,
                        ),
                    )
                    stats["changed"] += 1
                final_key, final_kind = identity_key, identity_kind

            for issue in parsed_entry.issues:
                _record_issue(
                    conn,
                    feed_id=feed_id,
                    entry_id=entry_id,
                    identity_key=final_key,
                    title=parsed_entry.title,
                    kind=issue["code"],
                    field=issue.get("field", ""),
                    detail=issue.get("detail", ""),
                    stamp=stamp,
                    observed=observed_issues,
                )
            if final_kind in ("content", "link"):
                _record_issue(
                    conn,
                    feed_id=feed_id,
                    entry_id=entry_id,
                    identity_key=final_key,
                    title=parsed_entry.title,
                    kind="missing_identity",
                    field="",
                    detail=(
                        "entry has neither a stable id nor a link"
                        if final_kind == "content"
                        else "entry has no stable id; normalised link is used as identity"
                    ),
                    stamp=stamp,
                    observed=observed_issues,
                )
            seen_identities.add(final_key)

        # Entries not returned in this round are retained, just flagged.
        rows = conn.execute(
            "SELECT id, identity_key FROM entries WHERE feed_id = ?", (feed_id,)
        ).fetchall()
        for row in rows:
            if row["identity_key"] in seen_identities:
                continue
            conn.execute(
                """UPDATE entries SET missing_rounds = missing_rounds + 1
                   WHERE id = ?""",
                (row["id"],),
            )
            stats["missing"] += 1

        # Resolve issues that this round could actually re-check: those
        # attached to entries we just saw, and those whose entry is gone.
        # An entry that was simply absent from a truncated feed has NOT been
        # re-parsed, so its problem may well still exist; leaving it open is
        # the honest answer.
        active = conn.execute(
            """SELECT i.id, i.entry_id, i.identity_key, i.kind, i.field,
                      e.missing_rounds
               FROM entry_issues i
               LEFT JOIN entries e ON e.id = i.entry_id
               WHERE i.feed_id = ? AND i.resolved_at IS NULL""",
            (feed_id,),
        ).fetchall()
        for issue in active:
            triple = (issue["identity_key"], issue["kind"], issue["field"])
            if triple in observed_issues:
                continue
            if issue["entry_id"] is None:
                # An entry-level report with no stored entry (an entry that
                # could not be identified at all).  There is no row to check
                # absence against, so it is resolved only if this round did
                # not report it again.
                pass
            elif issue["missing_rounds"] is not None and issue["missing_rounds"] > 0:
                continue  # not present this round, nothing new learned
            conn.execute(
                "UPDATE entry_issues SET resolved_at = ? WHERE id = ?",
                (stamp, issue["id"]),
            )

        if parsed.title:
            conn.execute(
                "UPDATE feeds SET title = ?, site_link = ?, updated_at = ? WHERE id = ?",
                (parsed.title, parsed.link or "", stamp, feed_id),
            )

    return stats


def update_fetch(
    conn: sqlite3.Connection,
    fetch_id: int,
    *,
    status: str | None = None,
    stats: dict[str, int] | None = None,
    error_code: str | None = None,
) -> None:
    """Finish a fetch log row once ingestion has committed (or failed)."""
    fields: dict[str, Any] = {}
    if status is not None:
        fields["status"] = status
    if error_code is not None:
        fields["error_code"] = error_code
    if stats is not None:
        fields["entries_seen"] = stats.get("seen", 0)
        fields["entries_new"] = stats.get("new", 0)
        fields["entries_changed"] = stats.get("changed", 0)
        fields["entries_missing"] = stats.get("missing", 0)
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    with transaction(conn):
        conn.execute(
            f"UPDATE feed_fetches SET {columns} WHERE id = ?", (*fields.values(), fetch_id)
        )


def record_fetch(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    http_status: int | None = None,
    not_modified: bool = False,
    stats: dict[str, int] | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    etag: str | None = None,
    source_version: str = "",
    keep: int = 200,
) -> int:
    stats = stats or {}
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO feed_fetches
                   (feed_id, started_at, finished_at, status, http_status, not_modified,
                    entries_seen, entries_new, entries_changed, entries_missing,
                    error_code, error_detail, duration_ms, etag, source_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id, iso(started_at), iso(finished_at), status, http_status,
                1 if not_modified else 0, stats.get("seen", 0), stats.get("new", 0),
                stats.get("changed", 0), stats.get("missing", 0), error_code,
                error_detail, int((finished_at - started_at).total_seconds() * 1000),
                etag, source_version,
            ),
        )
        fetch_id = int(cur.lastrowid)
        # Audit log is bounded; article/revision state is unaffected.
        conn.execute(
            """DELETE FROM feed_fetches WHERE feed_id = ? AND id <= (
                   SELECT id FROM feed_fetches WHERE feed_id = ?
                   ORDER BY id DESC LIMIT 1 OFFSET ?)""",
            (feed_id, feed_id, keep),
        )
    return fetch_id


# ---------------------------------------------------------------------------
# Queries for the API


def _feed_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "kind": row["kind"],
        "fixture_slug": row["fixture_slug"],
        "fixture_version": row["fixture_version"],
        "title": row["title"] or row["url"],
        "site_link": row["site_link"],
        "topic": row["topic"],
        "paused": bool(row["paused"]),
        "etag": row["etag"],
        "last_modified": row["last_modified"],
        "last_fetch_at": row["last_fetch_at"],
        "last_success_at": row["last_success_at"],
        "last_http_status": row["last_http_status"],
        "last_result": row["last_result"],
        "last_error_code": row["last_error_code"],
        "last_error_detail": row["last_error_detail"],
        "failure_count": row["failure_count"],
        "next_retry_at": row["next_retry_at"],
        "created_at": row["created_at"],
    }


def list_feeds_with_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    feeds = []
    for row in list_feeds(conn):
        item = _feed_summary(row)
        stats = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN missing_rounds > 0 THEN 1 ELSE 0 END) AS absent,
                      SUM(CASE WHEN (SELECT COUNT(*) FROM entry_revisions r
                                     WHERE r.entry_id = e.id) > 1
                               THEN 1 ELSE 0 END) AS revised
               FROM entries e WHERE e.feed_id = ?""",
            (row["id"],),
        ).fetchone()
        issues = conn.execute(
            "SELECT COUNT(*) FROM entry_issues WHERE feed_id = ? AND resolved_at IS NULL",
            (row["id"],),
        ).fetchone()[0]
        item["entry_count"] = stats["total"] or 0
        item["absent_count"] = stats["absent"] or 0
        item["revised_count"] = stats["revised"] or 0
        item["active_issues"] = issues
        feeds.append(item)
    return feeds


def list_entries(
    conn: sqlite3.Connection,
    *,
    topic: str | None = None,
    feed_id: int | None = None,
    search: str | None = None,
    include_absent: bool = True,
    limit: int = 500,
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if topic:
        where.append("f.topic = ?")
        params.append(topic)
    if feed_id:
        where.append("e.feed_id = ?")
        params.append(feed_id)
    if search:
        where.append("(e.title LIKE ? OR e.summary LIKE ? OR e.content_html LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if not include_absent:
        where.append("e.missing_rounds = 0")
    sql = f"""
        SELECT e.*, f.title AS feed_title, f.topic AS feed_topic,
               (SELECT COUNT(*) FROM entry_revisions r WHERE r.entry_id = e.id) AS revision_count,
               (SELECT COUNT(*) FROM entry_issues i
                 WHERE i.entry_id = e.id AND i.resolved_at IS NULL) AS active_issues
        FROM entries e JOIN feeds f ON f.id = e.feed_id
        {('WHERE ' + ' AND '.join(where)) if where else ''}
        ORDER BY COALESCE(e.published_at, e.first_seen_at) DESC, e.id DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        published = row["published_at"]
        republished = False
        if published and row["first_seen_at"]:
            try:
                pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
                seen = datetime.fromisoformat(row["first_seen_at"].replace("Z", "+00:00"))
                republished = (seen - pub) > timedelta(days=30)
            except ValueError:
                pass
        result.append(
            {
                "id": row["id"],
                "feed_id": row["feed_id"],
                "feed_title": row["feed_title"],
                "topic": row["feed_topic"],
                "title": row["title"],
                "link": row["link"],
                "published_at": published,
                "updated_at": row["updated_at"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "missing_rounds": row["missing_rounds"],
                "absent_from_latest": row["missing_rounds"] > 0,
                "revision_count": row["revision_count"],
                "active_issues": row["active_issues"],
                "republished": republished,
                "summary": row["summary"][:200],
                "identity_kind": row["identity_kind"],
            }
        )
    return result


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT e.*, f.title AS feed_title, f.topic AS feed_topic, f.url AS feed_url,
                  f.kind AS feed_kind, f.fixture_slug, f.fixture_version
           FROM entries e JOIN feeds f ON f.id = e.feed_id WHERE e.id = ?""",
        (entry_id,),
    ).fetchone()
    if row is None:
        return None
    revisions = conn.execute(
        """SELECT id, revision_no, title, content_html, published_at, updated_at,
                  observed_at, source_version
           FROM entry_revisions WHERE entry_id = ? ORDER BY revision_no""",
        (entry_id,),
    ).fetchall()
    links = conn.execute(
        "SELECT link, first_seen_at, last_seen_at FROM entry_links WHERE entry_id = ? ORDER BY first_seen_at",
        (entry_id,),
    ).fetchall()
    issues = conn.execute(
        """SELECT kind, field, detail, first_seen_at, last_seen_at, occurrences, resolved_at
           FROM entry_issues WHERE entry_id = ? ORDER BY kind, field""",
        (entry_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "feed_id": row["feed_id"],
        "feed_title": row["feed_title"],
        "feed_url": row["feed_url"],
        "feed_kind": row["feed_kind"],
        "topic": row["feed_topic"],
        "fixture_slug": row["fixture_slug"],
        "fixture_version": row["fixture_version"],
        "title": row["title"],
        "link": row["link"],
        "identity_key": row["identity_key"],
        "identity_kind": row["identity_kind"],
        "guid": row["guid"],
        "published_at": row["published_at"],
        "updated_at": row["updated_at"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "missing_rounds": row["missing_rounds"],
        "content_html": row["content_html"],
        "summary": row["summary"],
        "revisions": [dict(r) for r in revisions],
        "links": [dict(r) for r in links],
        "issues": [dict(i) for i in issues],
    }


def feed_detail(conn: sqlite3.Connection, feed_id: int) -> dict[str, Any] | None:
    row = get_feed(conn, feed_id)
    if row is None:
        return None
    detail = _feed_summary(row)
    detail["fetches"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM feed_fetches WHERE feed_id = ? ORDER BY id DESC LIMIT 20",
            (feed_id,),
        ).fetchall()
    ]
    detail["issues"] = [
        dict(r)
        for r in conn.execute(
            """SELECT i.*, e.title AS current_entry_title
               FROM entry_issues i LEFT JOIN entries e ON e.id = i.entry_id
               WHERE i.feed_id = ? ORDER BY i.resolved_at IS NULL DESC, i.last_seen_at DESC
               LIMIT 100""",
            (feed_id,),
        ).fetchall()
    ]
    detail["entries"] = list_entries(conn, feed_id=feed_id, limit=1000)
    return detail


def all_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT i.*, f.title AS feed_title, f.topic AS feed_topic
           FROM entry_issues i JOIN feeds f ON f.id = i.feed_id
           ORDER BY i.resolved_at IS NULL DESC, i.last_seen_at DESC, i.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def record_feed_issue(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    kind: str,
    detail: str,
    stamp: str | None = None,
) -> None:
    stamp = stamp or now_iso()
    with transaction(conn):
        conn.execute(
            """INSERT INTO entry_issues
                   (feed_id, entry_id, identity_key, entry_title, kind, field, detail,
                    first_seen_at, last_seen_at, occurrences)
               VALUES (?, NULL, '', '', ?, '', ?, ?, ?, 1)
               ON CONFLICT(feed_id, identity_key, kind, field) DO UPDATE SET
                   detail = excluded.detail,
                   last_seen_at = excluded.last_seen_at,
                   occurrences = entry_issues.occurrences + 1,
                   resolved_at = NULL""",
            (feed_id, kind, detail, stamp, stamp),
        )


def resolve_feed_issues(
    conn: sqlite3.Connection, feed_id: int, kinds: Iterable[str] | None = None
) -> None:
    """Mark source-level issues as resolved.

    ``kinds=None`` resolves every source-level issue.  A successful fetch is
    proof that *any* previous source-level failure (404, 500, timeout, bad
    XML) has cleared, and failure kinds are stored per HTTP status, so an
    explicit kind list would silently leak the rest.
    """
    stamp = now_iso()
    with transaction(conn):
        if kinds is None:
            conn.execute(
                """UPDATE entry_issues SET resolved_at = ?
                   WHERE feed_id = ? AND identity_key = '' AND resolved_at IS NULL""",
                (stamp, feed_id),
            )
            return
        kinds = [k for k in kinds]
        if not kinds:
            return
        placeholders = ",".join("?" for _ in kinds)
        conn.execute(
            f"""UPDATE entry_issues SET resolved_at = ?
                WHERE feed_id = ? AND identity_key = '' AND resolved_at IS NULL
                  AND kind IN ({placeholders})""",
            (stamp, feed_id, *kinds),
        )


# ---------------------------------------------------------------------------
# Export


def export_entries(conn: sqlite3.Connection, entry_ids: list[int]) -> dict[str, Any]:
    entries = []
    feeds: dict[int, dict[str, Any]] = {}
    for entry_id in entry_ids:
        detail = get_entry(conn, entry_id)
        if detail is None:
            continue
        revisions = []
        for rev in detail["revisions"]:
            revisions.append(
                {
                    "revision_no": rev["revision_no"],
                    "title": rev["title"],
                    "content_html": rev["content_html"],
                    "content_text": strip_html(rev["content_html"]),
                    "published_at": rev["published_at"],
                    "updated_at": rev["updated_at"],
                    "observed_at": rev["observed_at"],
                    "source_version": rev["source_version"],
                }
            )
        entries.append(
            {
                "id": detail["id"],
                "title": detail["title"],
                "link": detail["link"],
                "topic": detail["topic"],
                "published_at": detail["published_at"],
                "updated_at": detail["updated_at"],
                "first_seen_at": detail["first_seen_at"],
                "last_seen_at": detail["last_seen_at"],
                "identity_key": detail["identity_key"],
                "identity_kind": detail["identity_kind"],
                "guid": detail["guid"],
                "content_html": detail["content_html"],
                "content_text": strip_html(detail["content_html"]),
                "links": detail["links"],
                "issues": detail["issues"],
                "revisions": revisions,
            }
        )
        feed_id = detail["feed_id"]
        if feed_id not in feeds:
            feed = get_feed(conn, feed_id)
            if feed is not None:
                fetches = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM feed_fetches WHERE feed_id = ? ORDER BY id DESC LIMIT 10",
                        (feed_id,),
                    ).fetchall()
                ]
                feeds[feed_id] = {
                    "id": feed["id"],
                    "url": feed["url"],
                    "title": feed["title"],
                    "topic": feed["topic"],
                    "kind": feed["kind"],
                    "fixture_slug": feed["fixture_slug"],
                    "fixture_version": feed["fixture_version"],
                    "last_fetch_at": feed["last_fetch_at"],
                    "last_success_at": feed["last_success_at"],
                    "last_result": feed["last_result"],
                    "fetches": fetches,
                }
    return {
        "exported_at": now_iso(),
        "count": len(entries),
        "entries": entries,
        "feeds": list(feeds.values()),
    }


def issue_label(kind: str) -> str:
    known = {
        "bad_date": "异常日期",
        "empty_body": "空正文",
        "bad_xml": "XML 解析失败",
        "missing_identity": "缺少稳定标识",
        "http_error": "HTTP 错误",
        "network_error": "网络错误",
    }
    if kind in known:
        return known[kind]
    match = re.fullmatch(r"http_(\d{3})", kind or "")
    if match:
        return f"源返回 HTTP {match.group(1)}"
    return kind or "错误"


def export_markdown(payload: dict[str, Any]) -> str:
    lines = ["# 订阅文章导出", "", f"导出时间：{payload['exported_at']}", ""]
    feeds_by_id = {feed["id"]: feed for feed in payload["feeds"]}
    for entry in payload["entries"]:
        lines.append(f"## {entry['title']}")
        lines.append("")
        lines.append(f"- 来源：{entry['link'] or '（无链接）'}")
        lines.append(f"- 话题：{entry['topic']}")
        lines.append(f"- 发布时间：{entry['published_at'] or '未知'}")
        lines.append(f"- 更新时间：{entry['updated_at'] or '未知'}")
        lines.append(f"- 首次收录：{entry['first_seen_at']}")
        lines.append(f"- 标识：`{entry['identity_key']}`（{entry['identity_kind']}）")
        lines.append(f"- 修订次数：{len(entry['revisions'])}")
        lines.append("")
        lines.append(entry["content_text"] or "（空正文）")
        lines.append("")
        lines.append("### 修订历史")
        lines.append("")
        for rev in entry["revisions"]:
            lines.append(
                f"- r{rev['revision_no']} · {rev['observed_at']} · "
                f"{rev['source_version'] or '—'} · 标题「{rev['title']}」"
            )
        if entry["issues"]:
            lines.append("")
            lines.append("### 问题记录")
            lines.append("")
            for issue in entry["issues"]:
                state = "已解决" if issue["resolved_at"] else "未解决"
                lines.append(
                    f"- {issue_label(issue['kind'])}"
                    f"（{state}，出现 {issue['occurrences']} 次）：{issue['detail']}"
                )
        lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("## 来源记录")
    lines.append("")
    for feed in payload["feeds"]:
        lines.append(f"### {feed['title'] or feed['url']}")
        lines.append("")
        lines.append(f"- 地址：`{feed['url']}`")
        lines.append(f"- 话题：{feed['topic']}")
        lines.append(f"- 最近结果：{feed['last_result']}（{feed['last_success_at'] or '尚未成功'}）")
        for fetch in feed["fetches"]:
            lines.append(
                f"- {fetch['started_at']} · {fetch['status']} · HTTP {fetch['http_status'] or '—'}"
                f" · 新增 {fetch['entries_new']} / 变更 {fetch['entries_changed']}"
                f" · 未见 {fetch['entries_missing']}"
            )
        lines.append("")
    return "\n".join(lines)


def revision_diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return a structured diff between two revisions.

    Line based so the UI can highlight exactly what changed.  We compare
    plain text, not raw markup: markup churn (a permalink wrapper, for
    example) should not drown out a real wording change.
    """
    import difflib

    def lines_of(rev: dict[str, Any]) -> list[str]:
        text = strip_html(rev.get("content_html") or "")
        return text.splitlines() or ([text] if text else [])

    left_lines = lines_of(left)
    right_lines = lines_of(right)
    diff = list(difflib.unified_diff(left_lines, right_lines, lineterm="", n=3))
    return {
        "left": {k: left.get(k) for k in ("revision_no", "title", "observed_at", "updated_at")},
        "right": {k: right.get(k) for k in ("revision_no", "title", "observed_at", "updated_at")},
        "title_changed": (left.get("title") or "") != (right.get("title") or ""),
        "left_title": left.get("title") or "",
        "right_title": right.get("title") or "",
        "left_text": strip_html(left.get("content_html") or ""),
        "right_text": strip_html(right.get("content_html") or ""),
        "unified": diff,
        "identical": left_lines == right_lines,
    }
