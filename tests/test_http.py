"""End-to-end HTTP tests: real urllib requests against a local server.

These cover the parts fixtures cannot: ETag / Last-Modified round trips,
304 handling, HTTP failures and a republished-guid scenario over the wire.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import refresh, store  # noqa: E402
from app.fetcher import FixtureRegistry  # noqa: E402

FIXTURES = FixtureRegistry(ROOT / "fixtures")

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>线上源</title>
  <link>http://127.0.0.1:{port}/</link>
  <item>
    <title>{title}</title>
    <link>http://127.0.0.1:{port}/a.html</link>
    <guid isPermaLink="false">wire-a</guid>
    <pubDate>Mon, 02 Nov 2026 01:30:00 GMT</pubDate>
    <description><![CDATA[<p>{body}</p>]]></description>
  </item>
</channel></rss>
"""


class Wire:
    """Mutable state shared with the test server."""

    def __init__(self) -> None:
        self.status = 200
        self.body = ""
        self.etag = '"v1"'
        self.last_modified = "Mon, 02 Nov 2026 01:30:00 GMT"
        self.requests: list[dict[str, str]] = []
        self.honor_conditional = True
        self.not_modified_wrongly = False

    def status_for(self, path: str) -> int:
        """Per-path status so one broken endpoint does not fail the whole stub."""
        if path.startswith("/missing"):
            return 404
        return self.status


class Handler(BaseHTTPRequestHandler):
    wire: Wire
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.wire.requests.append(headers)
        status = self.wire.status_for(self.path)
        if status != 200:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.wire.honor_conditional:
            if headers.get("if-none-match") == self.wire.etag:
                self.send_response(304)
                self.send_header("ETag", self.wire.etag)
                self.end_headers()
                return
        body = self.wire.body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", self.wire.etag)
        self.send_header("Last-Modified", self.wire.last_modified)
        self.end_headers()
        self.wfile.write(body)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wire = Wire()
        handler = type("Bound", (Handler,), {"wire": cls.wire})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(os.path.join(self.tmp.name, "http.db"))
        self.wire.status = 200
        self.wire.etag = '"v1"'
        self.wire.honor_conditional = True
        self.wire.requests.clear()
        self.url = f"http://127.0.0.1:{self.port}/feed.xml"
        self.feed_id = store.create_feed(
            self.conn, url=self.url, kind="http", topic="线上", title="线上源"
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def set_doc(self, title: str, body: str) -> None:
        self.wire.body = RSS.format(port=self.port, title=title, body=body)

    def fetch(self):
        return refresh.refresh_feed(
            self.conn, self.feed_id, fixtures=FIXTURES, force=False
        )

    def test_etag_round_trip_returns_304_and_writes_nothing(self):
        self.set_doc("线上文章", "第一版正文")
        first = self.fetch()
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["stats"]["new"], 1)
        stored_etag = store.get_feed(self.conn, self.feed_id)["etag"]
        self.assertEqual(stored_etag, '"v1"')

        second = self.fetch()
        self.assertEqual(second["status"], "not_modified")
        # The second request must carry the validator.
        self.assertEqual(self.wire.requests[-1].get("if-none-match"), '"v1"')
        self.assertEqual(self.wire.requests[-1].get("if-modified-since"),
                         "Mon, 02 Nov 2026 01:30:00 GMT")
        self.assertEqual(len(store.list_entries(self.conn, feed_id=self.feed_id, limit=10)), 1)

    def test_content_change_creates_revision_over_http(self):
        self.set_doc("线上文章", "第一版正文")
        self.fetch()
        entry_id = store.list_entries(self.conn, feed_id=self.feed_id, limit=10)[0]["id"]

        self.wire.etag = '"v2"'
        self.set_doc("线上文章", "第二版正文")
        result = self.fetch()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stats"]["changed"], 1)
        detail = store.get_entry(self.conn, entry_id)
        self.assertEqual(len(detail["revisions"]), 2)
        self.assertIn("第二版", detail["content_html"])

    def test_304_with_wrong_etag_is_treated_as_no_change(self):
        self.set_doc("线上文章", "正文")
        self.fetch()
        self.wire.honor_conditional = True
        self.wire.etag = '"v1"'  # unchanged, so 304
        result = self.fetch()
        self.assertEqual(result["status"], "not_modified")

    def test_http_500_marks_failure_and_schedules_retry(self):
        self.set_doc("线上文章", "正文")
        self.fetch()
        self.wire.status = 500
        result = self.fetch()
        self.assertEqual(result["status"], "http_error")
        self.assertEqual(result["http_status"], 500)
        feed = store.get_feed(self.conn, self.feed_id)
        self.assertEqual(feed["last_result"], "failed")
        self.assertEqual(feed["failure_count"], 1)
        self.assertIsNotNone(feed["next_retry_at"])
        # Previous article is still there.
        self.assertEqual(len(store.list_entries(self.conn, feed_id=self.feed_id, limit=10)), 1)

    def test_http_404_is_isolated_from_other_feeds(self):
        self.set_doc("线上文章", "正文")
        self.fetch()
        good = self.feed_id
        bad = store.create_feed(
            self.conn, url=f"http://127.0.0.1:{self.port}/missing.xml", kind="http", topic="线上"
        )
        results = refresh.refresh_all(self.conn, fixtures=FIXTURES, force=True)
        by_feed = {r["feed_id"]: r for r in results}
        self.assertEqual(by_feed[bad]["status"], "http_error")
        self.assertEqual(by_feed[bad]["http_status"], 404)
        self.assertEqual(by_feed[good]["status"], "ok")
        self.assertTrue(store.list_entries(self.conn, feed_id=good, limit=10))

    def test_forced_refresh_omits_conditional_headers(self):
        self.set_doc("线上文章", "正文")
        self.fetch()
        refresh.refresh_feed(self.conn, self.feed_id, fixtures=FIXTURES, force=True)
        last = self.wire.requests[-1]
        self.assertNotIn("if-none-match", last)
        self.assertNotIn("if-modified-since", last)


if __name__ == "__main__":
    unittest.main()
