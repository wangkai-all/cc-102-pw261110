"""HTTP entry point and JSON API.

Standard library only, so the project runs with ``python3 app/main.py`` and
no install step.  Each request gets its own SQLite connection; WAL mode plus
a write lock keeps concurrent UI polls safe.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import threading
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import fetcher, refresh, store
from .feedparse import FeedParseError, iso, normalize_url, parse_feed, strip_html
from .sanitize import sanitize_html

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
FIXTURE_DIR = BASE_DIR.parent / "fixtures"
DEFAULT_DB = BASE_DIR.parent / "data" / "topicfeed.db"

_write_lock = threading.Lock()


class App:
    def __init__(self, db_path: str, fixture_dir: str | os.PathLike[str] = FIXTURE_DIR):
        self.db_path = db_path
        self.fixtures = fetcher.FixtureRegistry(fixture_dir)
        self.scheduler: refresh.Scheduler | None = None

    def connect(self) -> sqlite3.Connection:
        return store.connect(self.db_path)

    def bootstrap(self) -> None:
        conn = self.connect()
        try:
            refresh.seed_fixture_feeds(conn, self.fixtures)
        finally:
            conn.close()

    def scheduler_status(self) -> dict[str, Any] | None:
        if self.scheduler is None:
            return None
        return {
            "interval_seconds": self.scheduler.interval,
            "last_run": self.scheduler.last_run,
            "last_results": self.scheduler.last_results,
        }


# ---------------------------------------------------------------------------
# Serialisation helpers


def _entry_out(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    detail = store.get_entry(conn, entry_id)
    if detail is None:
        return None
    detail["content_html"] = sanitize_html(detail["content_html"])
    for revision in detail["revisions"]:
        revision["content_html"] = sanitize_html(revision["content_html"])
        revision["content_text"] = strip_html(revision["content_html"])
    return detail


def _payload_state(app: App, params: dict[str, list[str]]) -> dict[str, Any]:
    def first(name: str, default: str | None = None) -> str | None:
        values = params.get(name)
        return values[0] if values else default

    topic = first("topic") or None
    feed_id = first("feed")
    search = first("q") or None
    include_absent = (first("absent") or "1") != "0"
    conn = app.connect()
    try:
        feeds = store.list_feeds_with_stats(conn)
        entries = store.list_entries(
            conn,
            topic=topic,
            feed_id=int(feed_id) if feed_id else None,
            search=search,
            include_absent=include_absent,
        )
        return {
            "feeds": feeds,
            "topics": store.topic_counts(conn),
            "entries": entries,
            "issues": [i for i in store.all_issues(conn) if i["resolved_at"] is None],
            "fixtures": app.fixtures.all_descriptions(),
            "filters": {"topic": topic, "feed": feed_id, "q": search, "absent": include_absent},
            "stats": {
                "feeds": len(feeds),
                "entries": sum(f["entry_count"] for f in feeds),
                "active_issues": sum(f["active_issues"] for f in feeds),
            },
            "scheduler": app.scheduler_status(),
            "server_time": store.now_iso(),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Request handler


class Handler(BaseHTTPRequestHandler):
    server_version = "TopicFeed/1.0"
    protocol_version = "HTTP/1.1"

    app: App  # set on the class by make_server

    # -- utilities ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
        if os.environ.get("TOPICFEED_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, code: str, detail: str) -> None:
        self._json(status, {"error": {"code": code, "detail": detail}})

    def _body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON 请求体必须是对象")
        return value

    def _path_parts(self) -> list[str]:
        parsed = urllib.parse.urlsplit(self.path)
        return [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/") if p]

    def _query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            parts = self._path_parts()
            if not parts:
                return self._serve_static("index.html")
            if parts[0] == "static":
                return self._serve_static("/".join(parts[1:]))
            if parts[0] != "api":
                return self._serve_static("index.html")
            return self._api_get(parts[1:])
        except Exception:
            traceback.print_exc()
            self._error(500, "internal_error", "服务器内部错误")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            parts = self._path_parts()
            if len(parts) < 2 or parts[0] != "api":
                return self._error(404, "not_found", "接口不存在")
            return self._api_post(parts[1:])
        except ValueError as exc:
            self._error(400, "bad_request", str(exc))
        except Exception:
            traceback.print_exc()
            self._error(500, "internal_error", "服务器内部错误")

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            parts = self._path_parts()
            if len(parts) < 2 or parts[0] != "api":
                return self._error(404, "not_found", "接口不存在")
            return self._api_delete(parts[1:])
        except Exception:
            traceback.print_exc()
            self._error(500, "internal_error", "服务器内部错误")

    # -- static files ------------------------------------------------------

    def _serve_static(self, relative: str) -> None:
        root = STATIC_DIR.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._error(404, "not_found", "文件不存在")
        if not target.is_file():
            return self._error(404, "not_found", "文件不存在")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif target.suffix in (".html", ".css"):
            content_type += "; charset=utf-8"
        if target.suffix in (".svg", ".png", ".ico"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            with target.open("rb") as handle:
                self.wfile.write(handle.read())
            return
        self._send(200, target.read_bytes(), content_type)

    # -- GET API -----------------------------------------------------------

    def _api_get(self, parts: list[str]) -> None:
        app = self.app
        if parts == ["state"]:
            return self._json(200, _payload_state(app, self._query()))
        if parts == ["fixtures"]:
            return self._json(200, {"fixtures": app.fixtures.all_descriptions()})
        if parts == ["issues"]:
            conn = app.connect()
            try:
                items = store.all_issues(conn)
            finally:
                conn.close()
            return self._json(200, {"issues": items})
        if len(parts) == 2 and parts[0] == "feeds":
            return self._feed_detail(int(parts[1]))
        if len(parts) == 2 and parts[0] == "entries":
            conn = app.connect()
            try:
                detail = _entry_out(conn, int(parts[1]))
            finally:
                conn.close()
            if detail is None:
                return self._error(404, "not_found", "文章不存在")
            return self._json(200, {"entry": detail})
        if len(parts) == 3 and parts[0] == "entries" and parts[2] == "diff":
            return self._entry_diff(int(parts[1]))
        if len(parts) == 3 and parts[0] == "fixtures":
            description = app.fixtures.describe(parts[1])
            if description is None:
                return self._error(404, "not_found", "未知演示源")
            return self._json(200, {"fixture": description})
        return self._error(404, "not_found", "接口不存在")

    def _feed_detail(self, feed_id: int) -> None:
        conn = self.app.connect()
        try:
            detail = store.feed_detail(conn, feed_id)
        finally:
            conn.close()
        if detail is None:
            return self._error(404, "not_found", "订阅源不存在")
        return self._json(200, {"feed": detail})

    def _entry_diff(self, entry_id: int) -> None:
        query = self._query()

        def rev_index(name: str, default: int) -> int:
            raw = (query.get(name) or [str(default)])[0]
            try:
                return int(raw)
            except ValueError:
                return default

        conn = self.app.connect()
        try:
            detail = store.get_entry(conn, entry_id)
            if detail is None:
                return self._error(404, "not_found", "文章不存在")
            revisions = detail["revisions"]
            if len(revisions) < 2:
                return self._error(400, "no_revisions", "该文章只有一个修订版本，无法对比")
            left_no = rev_index("left", len(revisions) - 1)
            right_no = rev_index("right", len(revisions))
            by_no = {r["revision_no"]: r for r in revisions}
            left = by_no.get(left_no)
            right = by_no.get(right_no)
            if left is None or right is None:
                return self._error(404, "not_found", "修订版本不存在")
            for revision in (left, right):
                revision["content_html"] = sanitize_html(revision["content_html"])
            diff = store.revision_diff(left, right)
        finally:
            conn.close()
        return self._json(200, {"diff": diff, "revisions": revisions})

    # -- POST API ----------------------------------------------------------

    def _api_post(self, parts: list[str]) -> None:
        app = self.app
        body = self._body_json()

        if parts == ["feeds"]:
            return self._create_feed(body)
        if parts == ["refresh"]:
            return self._refresh_all(body)
        if parts == ["export"]:
            return self._export(body)
        if parts == ["preview"]:
            return self._preview(body)

        if len(parts) >= 2 and parts[0] == "feeds":
            feed_id = int(parts[1])
            action = parts[2] if len(parts) > 2 else ""
            if action == "refresh":
                return self._refresh_one(feed_id, body)
            if action == "pause":
                with _write_lock:
                    conn = app.connect()
                    try:
                        refresh.set_paused(conn, feed_id, bool(body.get("paused")))
                    finally:
                        conn.close()
                return self._json(200, {"ok": True, "paused": bool(body.get("paused"))})
            if action == "retry":
                with _write_lock:
                    conn = app.connect()
                    try:
                        refresh.retry_feed(conn, feed_id)
                        result = refresh.refresh_feed(
                            conn, feed_id, fixtures=app.fixtures, force=True
                        )
                    finally:
                        conn.close()
                return self._json(200, {"ok": True, "result": result})
            if action == "clear-backoff":
                with _write_lock:
                    conn = app.connect()
                    try:
                        refresh.retry_feed(conn, feed_id)
                    finally:
                        conn.close()
                return self._json(200, {"ok": True, "next_retry_at": None})
            if action == "version":
                version = str(body.get("version") or "")
                if not version:
                    raise ValueError("缺少 version 参数")
                with _write_lock:
                    conn = app.connect()
                    try:
                        feed = store.get_feed(conn, feed_id)
                        if feed is None:
                            return self._error(404, "not_found", "订阅源不存在")
                        description = app.fixtures.describe(feed["fixture_slug"] or "")
                        known = {v["id"] for v in (description or {}).get("versions", [])}
                        if known and version not in known:
                            return self._error(
                                400, "invalid", f"该演示源没有版本 {version!r}"
                            )
                        changed = refresh.set_fixture_version(conn, feed_id, version)
                        if changed.get("status") != "ok":
                            return self._error(400, "invalid", changed.get("detail", "无法设置版本"))
                        result = refresh.refresh_feed(
                            conn,
                            feed_id,
                            fixtures=app.fixtures,
                            version=version,
                            force=True,
                        )
                    finally:
                        conn.close()
                return self._json(200, {"ok": True, "result": result, "version": version})
            if action == "reset":
                return self._reset_feed(feed_id)
        return self._error(404, "not_found", "接口不存在")

    def _create_feed(self, body: dict[str, Any]) -> None:
        url = str(body.get("url") or "").strip()
        if not url:
            raise ValueError("缺少 url 参数")
        if not re.match(r"^(https?|fixture)://", url, re.I):
            raise ValueError("地址必须以 http://、https:// 或 fixture:// 开头")
        if not url.lower().startswith("fixture://"):
            # Catch a broken port/IPv6 literal or a stray space up front,
            # instead of storing a subscription that can only ever fail.
            if not normalize_url(url):
                raise ValueError("地址不是可用的 http/https 链接（端口或主机格式有误）")
        topic = str(body.get("topic") or "未分类").strip() or "未分类"
        kind = "fixture" if url.lower().startswith("fixture://") else "http"
        slug = None
        version = None
        if kind == "fixture":
            parsed = urllib.parse.urlsplit(url)
            slug = parsed.netloc or parsed.path.strip("/")
            version = (urllib.parse.parse_qs(parsed.query).get("version") or [None])[0]
            if self.app.fixtures.describe(slug) is None:
                raise ValueError(f"未知的内置演示源：{slug}")
        with _write_lock:
            conn = self.app.connect()
            try:
                if store.get_feed_by_url(conn, url) is not None:
                    return self._error(409, "duplicate", "该订阅已存在，无需重复添加")
                feed_id = store.create_feed(
                    conn,
                    url=url,
                    kind=kind,
                    fixture_slug=slug,
                    fixture_version=version,
                    topic=topic,
                    title=(self.app.fixtures.describe(slug) or {}).get("title", "") if slug else "",
                )
                result = refresh.refresh_feed(conn, feed_id, fixtures=self.app.fixtures)
            finally:
                conn.close()
        return self._json(201, {"ok": True, "feed_id": feed_id, "result": result})

    def _refresh_one(self, feed_id: int, body: dict[str, Any]) -> None:
        version = body.get("version")
        with _write_lock:
            conn = self.app.connect()
            try:
                result = refresh.refresh_feed(
                    conn,
                    feed_id,
                    fixtures=self.app.fixtures,
                    force=True,
                    version=str(version) if version else None,
                )
            finally:
                conn.close()
        return self._json(200, {"ok": True, "result": result})

    def _refresh_all(self, body: dict[str, Any]) -> None:
        force = bool(body.get("force"))
        with _write_lock:
            conn = self.app.connect()
            try:
                results = refresh.refresh_all(
                    conn, fixtures=self.app.fixtures, force=force, only_due=not force
                )
            finally:
                conn.close()
        return self._json(200, {"ok": True, "results": results})

    def _reset_feed(self, feed_id: int) -> None:
        with _write_lock:
            conn = self.app.connect()
            try:
                feed = store.get_feed(conn, feed_id)
                if feed is None:
                    return self._error(404, "not_found", "订阅源不存在")
                store.delete_feed(conn, feed_id)
                feed_id = store.create_feed(
                    conn,
                    url=feed["url"],
                    kind=feed["kind"],
                    fixture_slug=feed["fixture_slug"],
                    fixture_version=feed["fixture_version"],
                    topic=feed["topic"],
                    title=feed["title"],
                )
                result = refresh.refresh_feed(conn, feed_id, fixtures=self.app.fixtures, force=True)
            finally:
                conn.close()
        return self._json(200, {"ok": True, "feed_id": feed_id, "result": result})

    def _preview(self, body: dict[str, Any]) -> None:
        """Parse a feed without storing it.

        Used by the "validate address" button before subscribing.  ``url`` is
        accepted as a base for relative links; ``xml`` may be omitted, in
        which case the URL itself is fetched (http/https or fixture).
        """
        raw = body.get("xml")
        base = body.get("url") or None
        if raw is None:
            if not base:
                raise ValueError("需要提供 xml 或 url")
            if base.lower().startswith("fixture://"):
                parts = urllib.parse.urlsplit(base)
                slug = parts.netloc or parts.path.strip("/")
                version = (urllib.parse.parse_qs(parts.query).get("version") or [None])[0]
                try:
                    raw_bytes = self.app.fixtures.read(slug, version)
                except (OSError, FeedParseError) as exc:
                    return self._json(200, {"ok": False, "error": {"code": "bad_fixture", "detail": str(exc)}})
            else:
                if not re.match(r"^https?://", base, re.I):
                    raise ValueError("地址必须以 http:// 或 https:// 开头")
                outcome = fetcher.fetch_feed(
                    {"url": base, "kind": "http"}, fixtures=self.app.fixtures, force=True
                )
                if not outcome.ok:
                    return self._json(
                        200,
                        {
                            "ok": False,
                            "error": {
                                "code": outcome.error_code or "fetch_error",
                                "detail": outcome.error_detail or outcome.status,
                            },
                        },
                    )
                raw_bytes = None
                parsed = outcome.parsed
                assert parsed is not None
                return self._json(200, self._preview_payload(parsed))
        else:
            if not isinstance(raw, str):
                raise ValueError("xml 必须是字符串")
            raw_bytes = raw.encode("utf-8")
        try:
            parsed = parse_feed(raw_bytes, base_url=base)
        except FeedParseError as exc:
            return self._json(200, {"ok": False, "error": {"code": "bad_xml", "detail": str(exc)}})
        return self._json(200, self._preview_payload(parsed))

    @staticmethod
    def _preview_payload(parsed: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "title": parsed.title,
            "link": parsed.link,
            "entries": [
                {
                    "title": e.title,
                    "link": e.link,
                    "guid": e.guid,
                    "published_at": iso(e.published),
                    "updated_at": iso(e.updated),
                    "issues": e.issues,
                }
                for e in parsed.entries
            ],
        }

    def _export(self, body: dict[str, Any]) -> None:
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("ids 必须是非空数组")
        fmt = str(body.get("format") or "json").lower()
        if fmt not in ("json", "markdown", "md"):
            raise ValueError("format 只能是 json 或 markdown")
        conn = self.app.connect()
        try:
            payload = store.export_entries(conn, [int(i) for i in ids])
        finally:
            conn.close()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if fmt == "json":
            data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            content_type = "application/json; charset=utf-8"
            filename = f"topicfeed-export-{stamp}.json"
        else:
            data = store.export_markdown(payload).encode("utf-8")
            content_type = "text/markdown; charset=utf-8"
            filename = f"topicfeed-export-{stamp}.md"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- DELETE ------------------------------------------------------------

    def _api_delete(self, parts: list[str]) -> None:
        if len(parts) == 2 and parts[0] == "feeds":
            with _write_lock:
                conn = self.app.connect()
                try:
                    store.delete_feed(conn, int(parts[1]))
                finally:
                    conn.close()
            return self._json(200, {"ok": True})
        return self._error(404, "not_found", "接口不存在")


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: str = str(DEFAULT_DB),
    *,
    start_scheduler: bool = True,
    scheduler_interval: float = 20.0,
) -> ThreadingHTTPServer:
    app = App(db_path)
    app.bootstrap()
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    if start_scheduler:
        app.scheduler = refresh.Scheduler(app, interval=scheduler_interval)
        app.scheduler.start()
        server.scheduler = app.scheduler  # type: ignore[attr-defined]
    return server


def main() -> None:
    host = os.environ.get("TOPICFEED_HOST", "127.0.0.1")
    port = int(os.environ.get("TOPICFEED_PORT", "8765"))
    db_path = os.environ.get("TOPICFEED_DB", str(DEFAULT_DB))
    interval = float(os.environ.get("TOPICFEED_INTERVAL", "300"))
    no_scheduler = os.environ.get("TOPICFEED_NO_SCHEDULER") == "1"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    server = make_server(
        host,
        port,
        db_path,
        start_scheduler=not no_scheduler,
        scheduler_interval=interval,
    )
    print(f"TopicFeed listening on http://{host}:{port}  (db: {db_path})")
    if not no_scheduler:
        print(f"Background refresh every {interval:g}s; TOPICFEED_NO_SCHEDULER=1 to disable")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if getattr(server, "scheduler", None) is not None:
            server.scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
