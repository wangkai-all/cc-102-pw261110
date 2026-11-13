"""End-to-end tests for identity, revisions, retention and failure isolation.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import refresh, store  # noqa: E402
from app.feedparse import (  # noqa: E402
    FeedParseError,
    iso as iso_dt,
    normalize_url,
    parse_date,
    parse_feed,
    strip_html,
)
from app.fetcher import FixtureRegistry, fetch_feed, fixture_url  # noqa: E402
from app.sanitize import sanitize_html  # noqa: E402

FIXTURES = FixtureRegistry(ROOT / "fixtures")


class DBTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = store.connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def add_fixture_feed(self, slug: str, version: str, topic: str = "测试") -> int:
        description = FIXTURES.describe(slug)
        assert description is not None
        return store.create_feed(
            self.conn,
            url=fixture_url(slug, version),
            kind="fixture",
            fixture_slug=slug,
            fixture_version=version,
            topic=topic,
            title=description["title"],
        )

    def refresh(self, feed_id: int, version: str | None = None, force: bool = True):
        return refresh.refresh_feed(
            self.conn, feed_id, fixtures=FIXTURES, version=version, force=force
        )

    def entries_by_title(self, feed_id: int | None = None):
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=1000)
        result: dict[str, dict] = {}
        for row in rows:
            result.setdefault(row["title"], []).append(row)
        return result

    def entry_title(self, entry_id: int) -> str:
        return store.get_entry(self.conn, entry_id)["title"]


# ---------------------------------------------------------------------------


class IdentityPromotionTests(DBTestCase):
    """A source that starts publishing stronger identifiers must not fork."""

    def _ingest(self, feed_id: int, xml: str):
        parsed = parse_feed(xml.encode("utf-8"))
        return store.ingest(self.conn, feed_id, parsed, source_version="inline")

    def _mk_feed(self) -> int:
        return store.create_feed(
            self.conn, url="https://promo.example.com/feed.xml", kind="http", topic="测试"
        )

    def test_guid_appearing_later_keeps_history_and_identity(self):
        feed_id = self._mk_feed()
        without_guid = """<?xml version="1.0"?><rss version="2.0"><channel>
          <title>P</title><link>https://promo.example.com/</link>
          <item><title>第一篇</title><link>https://promo.example.com/p1</link>
            <pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>旧正文</p>]]></description></item>
        </channel></rss>"""
        with_guid = """<?xml version="1.0"?><rss version="2.0"><channel>
          <title>P</title><link>https://promo.example.com/</link>
          <item><title>第一篇（改）</title><link>https://promo.example.com/p1</link>
            <guid>stable-1</guid>
            <pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>新正文</p>]]></description></item>
        </channel></rss>"""
        self._ingest(feed_id, without_guid)
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=10)
        self.assertEqual(len(rows), 1)
        entry_id = rows[0]["id"]
        self.assertEqual(rows[0]["identity_kind"], "link")

        stats = self._ingest(feed_id, with_guid)
        self.assertEqual(stats["new"], 0, "must not create a duplicate article")
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(stats["missing"], 0, "promoted row must not look absent")
        detail = store.get_entry(self.conn, entry_id)
        self.assertEqual(detail["identity_kind"], "guid")
        self.assertEqual(detail["identity_key"], "guid:stable-1")
        self.assertEqual(len(detail["revisions"]), 2)
        self.assertEqual(len(store.list_entries(self.conn, feed_id=feed_id, limit=10)), 1)
        # The link-fallback issue is resolved once the guid appears.
        self.assertTrue(all(i["resolved_at"] for i in detail["issues"]))

    def test_guid_dropped_keeps_article_via_link(self):
        feed_id = self._mk_feed()
        with_guid = """<?xml version="1.0"?><rss version="2.0"><channel><title>P</title>
          <item><title>文章</title><link>https://promo.example.com/a</link>
            <guid>g-1</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>正文</p>]]></description></item></channel></rss>"""
        without_guid = """<?xml version="1.0"?><rss version="2.0"><channel><title>P</title>
          <item><title>文章</title><link>https://promo.example.com/a</link>
            <pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>正文</p>]]></description></item></channel></rss>"""
        self._ingest(feed_id, with_guid)
        entry_id = store.list_entries(self.conn, feed_id=feed_id, limit=10)[0]["id"]
        stats = self._ingest(feed_id, without_guid)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(len(store.list_entries(self.conn, feed_id=feed_id, limit=10)), 1)
        # Identity stays on the guid we already know.
        self.assertEqual(store.get_entry(self.conn, entry_id)["identity_key"], "guid:g-1")

    def test_no_id_no_link_uses_fingerprint(self):
        feed_id = self._mk_feed()
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>P</title>
          <item><title>无标识</title><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>正文内容</p>]]></description></item></channel></rss>"""
        self._ingest(feed_id, xml)
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=10)
        self.assertEqual(rows[0]["identity_kind"], "content")
        self._ingest(feed_id, xml)
        self.assertEqual(len(store.list_entries(self.conn, feed_id=feed_id, limit=10)), 1)

    def test_same_title_different_body_status_no_merge_without_ids(self):
        feed_id = self._mk_feed()
        base = """<?xml version="1.0"?><rss version="2.0"><channel><title>P</title>
          <item><title>同名</title><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>{body}</p>]]></description></item></channel></rss>"""
        self._ingest(feed_id, base.format(body="甲"))
        self._ingest(feed_id, base.format(body="乙"))
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=10)
        self.assertEqual(len(rows), 2, "title alone must never merge two articles")


class DateTests(unittest.TestCase):
    def test_rfc822(self):
        value, error = parse_date("Wed, 19 Jun 2019 08:00:00 GMT")
        self.assertIsNone(error)
        self.assertEqual(value, datetime(2019, 6, 19, 8, tzinfo=timezone.utc))

    def test_iso_with_offset(self):
        value, error = parse_date("2026-11-02T09:30:00+08:00")
        self.assertIsNone(error)
        self.assertEqual(value, datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc))

    def test_date_only_is_rejected_not_guessed(self):
        value, error = parse_date("2026-11-11")
        self.assertIsNone(value)
        self.assertIn("ambiguous", error)

    def test_garbage_date_is_rejected(self):
        value, error = parse_date("2026年11月11日 星期三")
        self.assertIsNone(value)
        self.assertIn("unparseable", error)

    def test_rfc822_variants_that_real_feeds_use(self):
        expected = datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)
        for raw in (
            "Mon, 02 Nov 2026 01:30:00 +0000",
            "Mon, 02 Nov 2026 01:30:00 UTC",
            "Monday, 02 Nov 2026 01:30:00 GMT",
            "Mon, 02 Nov 2026 01:30:00 GMT (comment)",
            "Mon, 02 Nov 2026 01:30 GMT",
        ):
            value, error = parse_date(raw)
            self.assertIsNone(error, raw)
            self.assertEqual(value, expected, raw)

    def test_named_zone_offset_is_applied(self):
        value, error = parse_date("02 Nov 2026 01:30:00 PDT")
        self.assertIsNone(error)
        self.assertEqual(value, datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc))

    def test_two_digit_year_is_rejected_not_guessed(self):
        value, error = parse_date("Mon, 02 Nov 01 01:30:00 +0800")
        self.assertIsNone(value)
        self.assertIn("two-digit", error)

    def test_impossible_clock_values_are_rejected(self):
        self.assertIsNone(parse_date("Mon, 32 Nov 2026 01:00:00 GMT")[0])
        self.assertIsNone(parse_date("Mon, 02 Nov 2026 25:99:00 GMT")[0])

    def test_offset_that_would_overflow_datetime_is_a_bad_date(self):
        # Syntactically valid, but converting to UTC would pass datetime.max.
        value, error = parse_date("9999-12-31T23:59:59-05:00")
        self.assertIsNone(value)
        self.assertIn("out of range", error)

    def test_one_overflowing_date_does_not_abort_the_document(self):
        xml = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>A</title><link href="https://a.example/"/>
          <entry><title>good</title><id>tag:1</id>
            <updated>2026-11-02T01:00:00Z</updated>
            <content type="html">&lt;p&gt;ok&lt;/p&gt;</content></entry>
          <entry><title>edge</title><id>tag:2</id>
            <updated>9999-12-31T23:59:59-05:00</updated>
            <content type="html">&lt;p&gt;ok2&lt;/p&gt;</content></entry>
        </feed>"""
        entries = parse_feed(xml).entries
        self.assertEqual([e.title for e in entries], ["good", "edge"])
        self.assertIsNone(entries[1].published)
        self.assertTrue(any(i["code"] == "bad_date" for i in entries[1].issues))

    def test_lowercase_z_is_accepted(self):
        self.assertEqual(
            parse_date("2026-11-02T01:30:00z")[0],
            datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc),
        )

    def test_missing(self):
        self.assertEqual(parse_date(None), (None, "missing"))


class UrlTests(unittest.TestCase):
    def test_tracking_params_removed(self):
        self.assertEqual(
            normalize_url("https://example.com/a?utm_source=x&id=7&fbclid=y"),
            "https://example.com/a?id=7",
        )

    def test_case_and_default_port(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.COM:443/Path/"),
            "https://example.com/Path",
        )

    def test_non_tracking_query_kept_and_sorted(self):
        self.assertEqual(
            normalize_url("https://example.com/x?b=2&a=1"),
            "https://example.com/x?a=1&b=2",
        )

    def test_fragment_dropped(self):
        self.assertEqual(normalize_url("https://example.com/x#top"), "https://example.com/x")

    def test_relative_resolved(self):
        self.assertEqual(
            normalize_url("/posts/1", "https://example.com/feed.xml"),
            "https://example.com/posts/1",
        )


class LinkSafetyTests(unittest.TestCase):
    """A malformed or non-web link must never break the parse or reach the UI."""

    def test_hand_edited_port_does_not_abort_the_document(self):
        xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
          <link>https://ok.example/</link>
          <item><title>bad port</title><link>http://example.com:99999/x</link>
            <guid>g1</guid><pubDate>Thu, 13 Nov 2026 02:30:00 GMT</pubDate>
            <description><![CDATA[<p>body</p>]]></description></item>
        </channel></rss>"""
        feed = parse_feed(xml)  # must not raise
        self.assertEqual(len(feed.entries), 1)
        self.assertEqual(feed.entries[0].link, "")
        self.assertEqual(feed.entries[0].title, "bad port")

    def test_broken_ipv6_does_not_abort_the_document(self):
        for broken in ("http://[::1/x", "http://[zz]/x", "http://example.com:-1/x"):
            self.assertEqual(normalize_url(broken, "https://ok.example/"), "")

    def test_valid_ipv6_link_keeps_its_brackets(self):
        self.assertEqual(normalize_url("http://[::1]:8080/x"), "http://[::1]:8080/x")
        self.assertEqual(normalize_url("http://[::1]:80/x"), "http://[::1]/x")

    def test_host_with_a_space_is_not_a_link(self):
        self.assertEqual(normalize_url("http://exa mple.com/feed"), "")
        # A space in the path is legal and must survive.
        self.assertEqual(normalize_url("http://ok.example/a b"), "http://ok.example/a b")

    def test_non_web_scheme_is_not_an_identity(self):
        self.assertEqual(normalize_url("javascript:alert(1)"), "")
        self.assertEqual(normalize_url("data:text/html,<script>"), "")
        self.assertEqual(normalize_url("mailto:someone@example.com"), "")

    def test_non_web_channel_link_cannot_become_a_base(self):
        xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
          <link>javascript:alert(1)</link>
          <item><title>a</title><link>/posts/1</link><guid>g</guid>
            <pubDate>Thu, 13 Nov 2026 02:30:00 GMT</pubDate>
            <description><![CDATA[<p>b</p>]]></description></item>
        </channel></rss>"""
        feed = parse_feed(xml)
        self.assertEqual(feed.link, "")
        self.assertEqual(feed.entries[0].link, "")

    def test_broken_ipv6_atom_link_still_parses(self):
        xml = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>t</title><link href="https://ok.example/feed"/>
          <entry><title>a</title><id>x</id><updated>2026-11-13T02:30:00Z</updated>
            <link href="http://[::1/x" rel="alternate"/>
            <content type="html">&lt;p&gt;b&lt;/p&gt;</content></entry>
        </feed>"""
        feed = parse_feed(xml)
        self.assertEqual(feed.link, "https://ok.example/feed")
        self.assertEqual(feed.entries[0].link, "")


class AtomFallbackDateTests(unittest.TestCase):
    def test_bad_published_with_good_updated_falls_back_but_keeps_the_issue(self):
        xml = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>T</title><link href="https://a.example/"/>
          <entry><title>a</title><id>x</id><published>garbage</published>
            <updated>2026-11-02T01:00:00Z</updated>
            <content type="html">&lt;p&gt;b&lt;/p&gt;</content></entry>
        </feed>"""
        entry = parse_feed(xml).entries[0]
        self.assertEqual(iso_dt(entry.published), "2026-11-02T01:00:00Z")
        self.assertTrue(entry.published_is_fallback)
        self.assertTrue(any(i["code"] == "bad_date" for i in entry.issues),
                        "the broken field must stay diagnosable")

    def test_absent_published_is_not_an_issue(self):
        xml = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>T</title><link href="https://a.example/"/>
          <entry><title>a</title><id>x</id><updated>2026-11-02T01:00:00Z</updated>
            <content type="html">&lt;p&gt;b&lt;/p&gt;</content></entry>
        </feed>"""
        entry = parse_feed(xml).entries[0]
        self.assertTrue(entry.published_is_fallback)
        self.assertEqual(entry.issues, [])


class ParserTests(unittest.TestCase):
    def test_bad_xml_raises(self):
        raw = (ROOT / "fixtures/error-lab/v1.xml").read_bytes()
        with self.assertRaises(FeedParseError):
            parse_feed(raw)

    def test_atom_parsed(self):
        raw = (ROOT / "fixtures/design-notes/v1.xml").read_bytes()
        feed = parse_feed(raw)
        self.assertEqual(len(feed.entries), 2)
        self.assertTrue(feed.entries[0].guid.startswith("tag:"))

    def test_entry_level_issues_do_not_abort_document(self):
        raw = (ROOT / "fixtures/error-lab/v3.xml").read_bytes()
        feed = parse_feed(raw)
        codes = {(e.title, issue["code"]) for e in feed.entries for issue in e.issues}
        self.assertIn(("日期写成中文的条目", "bad_date"), codes)
        self.assertIn(("正文为空的条目", "empty_body"), codes)
        self.assertIn("一次真实的限流事故", [e.title for e in feed.entries])


class SanitizerTests(unittest.TestCase):
    def test_scripts_and_handlers_removed(self):
        dirty = '<p onclick="evil()">hi<script>alert(1)</script></p><img src="javascript:x" onerror="x()">'
        clean = sanitize_html(dirty)
        self.assertNotIn("script", clean.lower())
        self.assertNotIn("onerror", clean.lower())
        self.assertNotIn("javascript:", clean.lower())
        self.assertIn("<p>hi</p>", clean)

    def test_normal_markup_kept(self):
        clean = sanitize_html('<p>Hello <strong>there</strong></p>')
        self.assertEqual(clean, "<p>Hello <strong>there</strong></p>")


# ---------------------------------------------------------------------------


class IdentityTests(DBTestCase):
    def test_guid_identifies_entry_across_link_change(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        before = self.entries_by_title()
        self.assertIn("调度器的三个反直觉细节", before)
        entry_id = before["调度器的三个反直觉细节"][0]["id"]

        # v3 moves the article to a new link but keeps its guid.
        self.refresh(feed_id, version="v3")
        after = self.entries_by_title()
        matches = after["调度器的三个反直觉细节"]
        self.assertEqual(len(matches), 1, "link change must not create a second article")
        self.assertEqual(matches[0]["id"], entry_id)
        detail = store.get_entry(self.conn, entry_id)
        self.assertTrue(detail["link"].endswith("/scheduler.html"))
        # Both links stay on record.
        self.assertEqual(len(detail["links"]), 2)
        self.assertIn("scheduler-old.html", detail["links"][0]["link"])

    def test_title_change_does_not_split_article(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        entry_id = self.entries_by_title()["用 SQLite 做事件存储"][0]["id"]
        self.refresh(feed_id, version="v3")
        detail = store.get_entry(self.conn, entry_id)
        self.assertEqual(detail["title"], "用 SQLite 做事件存储（修订标题）")
        self.assertEqual(len(self.entries_by_title(feed_id)["用 SQLite 做事件存储（修订标题）"]), 1)

    def test_same_title_different_guid_stays_separate(self):
        feed_id = self.add_fixture_feed("tech-daily", "v3")
        self.refresh(feed_id)
        matches = self.entries_by_title()["发布流程改版说明"]
        self.assertEqual(len(matches), 2, "identical titles must not be merged")
        self.assertEqual(len({m["id"] for m in matches}), 2)
        details = [store.get_entry(self.conn, m["id"]) for m in matches]
        self.assertNotEqual(details[0]["guid"], details[1]["guid"])
        self.assertIn("面向服务", details[0]["content_html"] + details[1]["content_html"])
        self.assertIn("面向客户端", details[0]["content_html"] + details[1]["content_html"])

    def test_link_fallback_used_when_no_guid(self):
        feed_id = self.add_fixture_feed("tech-daily", "v3")
        self.refresh(feed_id)
        row = self.entries_by_title()["灰度发布检查清单"][0]
        self.assertEqual(row["identity_kind"], "link")
        self.refresh(feed_id, version="v4")
        self.assertEqual(len(self.entries_by_title()["灰度发布检查清单"]), 1)

    def test_atom_entry_without_id_uses_link(self):
        feed_id = self.add_fixture_feed("design-notes", "v1")
        self.refresh(feed_id)
        self.refresh(feed_id, version="v3")
        row = self.entries_by_title()["空状态页面的写法"][0]
        self.assertEqual(row["identity_kind"], "link")
        detail = store.get_entry(self.conn, row["id"])
        issues = {i["kind"] for i in detail["issues"] if not i["resolved_at"]}
        self.assertIn("missing_identity", issues)

    def test_republished_old_article_keeps_old_published_date(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        self.refresh(feed_id, version="v2")
        row = self.entries_by_title()["数据库连接池的容量估算"][0]
        published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        self.assertEqual(published.year, 2019)
        first_seen = datetime.fromisoformat(row["first_seen_at"].replace("Z", "+00:00"))
        self.assertGreater((first_seen - published).days, 365)
        self.assertTrue(row["republished"])


class RevisionTests(DBTestCase):
    def test_body_change_appends_revision(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        entry_id = self.entries_by_title()["调度器的三个反直觉细节"][0]["id"]
        self.refresh(feed_id, version="v3")  # link change only
        self.refresh(feed_id, version="v4")  # real body change
        detail = store.get_entry(self.conn, entry_id)
        self.assertEqual(detail["revisions"][0]["revision_no"], 1)
        self.assertEqual(len(detail["revisions"]), 2)
        self.assertIn("把长任务拆小", detail["revisions"][1]["content_html"])
        self.assertNotIn("把长任务拆小", detail["revisions"][0]["content_html"])

    def test_title_only_change_is_a_revision(self):
        feed_id = self.add_fixture_feed("design-notes", "v1")
        self.refresh(feed_id)
        buttons = self.entries_by_title()["按钮的三种状态不必都用颜色区分"][0]
        self.refresh(feed_id, version="v2")
        detail = store.get_entry(self.conn, buttons["id"])
        self.assertEqual(len(detail["revisions"]), 2)
        self.assertNotEqual(detail["revisions"][0]["title"], detail["revisions"][1]["title"])
        # Body is unchanged between the two revisions.
        self.assertEqual(
            strip_html(detail["revisions"][0]["content_html"]),
            strip_html(detail["revisions"][1]["content_html"]),
        )
        diff = store.revision_diff(detail["revisions"][0], detail["revisions"][1])
        self.assertTrue(diff["title_changed"])
        self.assertTrue(diff["identical"])

    def test_updated_date_without_body_change_is_not_a_revision(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        self.refresh(feed_id, version="v2")
        old_id = self.entries_by_title()["数据库连接池的容量估算"][0]["id"]
        before = store.get_entry(self.conn, old_id)
        self.refresh(feed_id, version="v4")  # bumps <atom:updated> only
        detail = store.get_entry(self.conn, old_id)
        self.assertEqual(len(detail["revisions"]), 1, "timestamp-only change must not add a revision")
        self.assertNotEqual(detail["updated_at"], before["updated_at"])
        self.assertEqual(detail["published_at"], before["published_at"])

    def test_reingesting_identical_content_adds_nothing(self):
        feed_id = self.add_fixture_feed("tech-daily", "v4")
        self.refresh(feed_id, version="v4")
        before_entries = len(store.list_entries(self.conn, feed_id=feed_id, limit=1000))
        before_revisions = sum(
            len(store.get_entry(self.conn, e["id"])["revisions"])
            for e in store.list_entries(self.conn, feed_id=feed_id, limit=1000)
        )
        for _ in range(3):
            # v5 has a different ETag but identical content.
            self.refresh(feed_id, version="v5")
        after_entries = len(store.list_entries(self.conn, feed_id=feed_id, limit=1000))
        after_revisions = sum(
            len(store.get_entry(self.conn, e["id"])["revisions"])
            for e in store.list_entries(self.conn, feed_id=feed_id, limit=1000)
        )
        self.assertEqual(before_entries, after_entries)
        self.assertEqual(before_revisions, after_revisions)

    def test_diff_reports_changed_lines(self):
        feed_id = self.add_fixture_feed("tech-daily", "v3")
        self.refresh(feed_id)
        entry_id = self.entries_by_title()["调度器的三个反直觉细节"][0]["id"]
        self.refresh(feed_id, version="v4")
        detail = store.get_entry(self.conn, entry_id)
        diff = store.revision_diff(detail["revisions"][0], detail["revisions"][-1])
        self.assertFalse(diff["identical"])
        added = [line for line in diff["unified"] if line.startswith("+") and not line.startswith("+++")]
        self.assertTrue(any("长任务" in line for line in added))


class RetentionTests(DBTestCase):
    def test_entries_missing_from_latest_round_are_kept(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        entry_id = self.entries_by_title()["旧文：一次线上事故复盘"][0]["id"]
        self.refresh(feed_id, version="v3")  # v3 no longer returns this article
        detail = store.get_entry(self.conn, entry_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["missing_rounds"], 1)
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=1000)
        self.assertTrue(any(r["id"] == entry_id and r["absent_from_latest"] for r in rows))

    def test_entry_returns_in_later_round_clears_absence(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        entry_id = self.entries_by_title()["旧文：一次线上事故复盘"][0]["id"]
        self.refresh(feed_id, version="v3")
        self.refresh(feed_id, version="v1")
        self.assertEqual(store.get_entry(self.conn, entry_id)["missing_rounds"], 0)

    def test_absent_entries_can_be_filtered_out(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        self.refresh(feed_id, version="v3")
        visible = store.list_entries(self.conn, feed_id=feed_id, include_absent=False, limit=1000)
        titles = {r["title"] for r in visible}
        self.assertNotIn("旧文：一次线上事故复盘", titles)
        self.assertIn("调度器的三个反直觉细节", titles)


class FailureTests(DBTestCase):
    def test_bad_xml_records_error_and_keeps_previous_entries(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        count_before = len(store.list_entries(self.conn, feed_id=feed_id, limit=1000))

        bad_feed = self.add_fixture_feed("error-lab", "v1")
        self.refresh(bad_feed)
        feed = store.get_feed(self.conn, bad_feed)
        self.assertEqual(feed["last_result"], "parse_error")
        self.assertEqual(feed["last_error_code"], "bad_xml")
        self.assertIn("malformed XML", feed["last_error_detail"])
        detail = store.feed_detail(self.conn, bad_feed)
        self.assertTrue(any(i["kind"] == "bad_xml" and not i["resolved_at"] for i in detail["issues"]))
        # Good source untouched.
        self.assertEqual(
            len(store.list_entries(self.conn, feed_id=feed_id, limit=1000)), count_before
        )

    def test_http_404_sets_backoff(self):
        feed_id = self.add_fixture_feed("error-lab", "v404")
        result = self.refresh(feed_id)
        self.assertEqual(result["status"], "http_error")
        feed = store.get_feed(self.conn, feed_id)
        self.assertEqual(feed["last_http_status"], 404)
        self.assertGreater(feed["failure_count"], 0)
        self.assertIsNotNone(feed["next_retry_at"])

    def test_one_failure_does_not_stop_the_batch(self):
        good = self.add_fixture_feed("tech-daily", "v1")
        bad = self.add_fixture_feed("error-lab", "v500")
        results = refresh.refresh_all(self.conn, fixtures=FIXTURES, force=True)
        by_feed = {r["feed_id"]: r for r in results}
        self.assertEqual(by_feed[good]["status"], "ok")
        self.assertEqual(by_feed[bad]["status"], "http_error")
        self.assertTrue(store.list_entries(self.conn, feed_id=good, limit=10))

    def test_paused_feed_is_skipped_by_scheduler_but_manual_refresh_allowed(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        refresh.set_paused(self.conn, feed_id, True)
        results = refresh.refresh_all(self.conn, fixtures=FIXTURES, force=True)
        self.assertEqual(results[0]["status"], "paused")
        self.assertIsNone(store.get_feed(self.conn, feed_id)["last_fetch_at"])
        manual = self.refresh(feed_id)
        self.assertEqual(manual["status"], "ok")

    def test_retry_backoff_blocks_automatic_retry(self):
        feed_id = self.add_fixture_feed("error-lab", "v500")
        self.refresh(feed_id)
        results = refresh.refresh_all(self.conn, fixtures=FIXTURES, only_due=True, respect_pause=False)
        self.assertEqual(results[0]["status"], "retry_scheduled")

    def test_error_fixture_v3_keeps_good_entry_alongside_bad(self):
        feed_id = self.add_fixture_feed("error-lab", "v3")
        self.refresh(feed_id)
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=100)
        titles = {r["title"] for r in rows}
        self.assertIn("一次真实的限流事故", titles)
        healthy = [r for r in rows if r["title"] == "一次真实的限流事故"][0]
        bad = [r for r in rows if r["title"] == "日期写成中文的条目"][0]
        self.assertEqual(healthy["active_issues"], 0)
        self.assertGreater(bad["active_issues"], 0)
        # A bad date must not be replaced by the fetch time.
        self.assertIsNone(bad["published_at"])

    def test_bad_date_detail_is_queryable(self):
        feed_id = self.add_fixture_feed("error-lab", "v2")
        self.refresh(feed_id)
        detail = store.feed_detail(self.conn, feed_id)
        bad_date = [i for i in detail["issues"] if i["kind"] == "bad_date"]
        self.assertEqual(len(bad_date), 2)
        details = " ".join(i["detail"] for i in bad_date)
        self.assertIn("unparseable date", details)
        self.assertIn("ambiguous", details)


class ConditionalRequestTests(DBTestCase):
    def test_fixture_304_when_etag_matches(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        first = self.refresh(feed_id)
        self.assertEqual(first["status"], "ok")
        second = self.refresh(feed_id, force=False)
        self.assertEqual(second["status"], "not_modified")
        self.assertEqual(second["http_status"], 304)
        # A 304 writes no revisions and no entries.
        self.assertEqual(len(store.list_entries(self.conn, feed_id=feed_id, limit=100)), 3)

    def test_version_switch_bypasses_validator(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        result = self.refresh(feed_id, version="v2")
        self.assertEqual(result["status"], "ok")

    def test_forced_refresh_does_not_take_304_shortcut(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        result = self.refresh(feed_id, force=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stats"]["new"], 0, "re-reading identical content adds nothing")
        self.assertEqual(result["stats"]["changed"], 0)

    def test_fetch_log_records_both_rounds(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        self.refresh(feed_id, force=False)
        rows = self.conn.execute(
            "SELECT status, not_modified, entries_new FROM feed_fetches WHERE feed_id = ? "
            "ORDER BY id",
            (feed_id,),
        ).fetchall()
        self.assertEqual([r["status"] for r in rows], ["ok", "not_modified"])
        self.assertEqual(rows[0]["entries_new"], 3)
        self.assertEqual(rows[1]["entries_new"], 0)

    def test_ingestion_failure_is_logged_honestly(self):
        from unittest import mock

        feed_id = self.add_fixture_feed("tech-daily", "v1")
        with mock.patch.object(store, "ingest", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.refresh(feed_id)
        row = self.conn.execute(
            "SELECT status, error_code FROM feed_fetches WHERE feed_id = ? ORDER BY id DESC",
            (feed_id,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "ingest_error")
        feed = store.get_feed(self.conn, feed_id)
        self.assertNotEqual(feed["last_result"], "ok")


class ExportTests(DBTestCase):
    def test_export_includes_revisions_and_feed_record(self):
        feed_id = self.add_fixture_feed("tech-daily", "v1")
        self.refresh(feed_id)
        entry_id = self.entries_by_title()["调度器的三个反直觉细节"][0]["id"]
        self.refresh(feed_id, version="v4")
        payload = store.export_entries(self.conn, [entry_id])
        self.assertEqual(payload["count"], 1)
        entry = payload["entries"][0]
        self.assertEqual(len(entry["revisions"]), 2)
        self.assertTrue(entry["content_text"])
        self.assertEqual(payload["feeds"][0]["id"], feed_id)
        self.assertTrue(payload["feeds"][0]["fetches"])
        markdown = store.export_markdown(payload)
        self.assertIn("修订历史", markdown)
        self.assertIn("来源记录", markdown)
        self.assertIn("调度器的三个反直觉细节", markdown)

    def test_export_unknown_id_is_skipped(self):
        payload = store.export_entries(self.conn, [9999])
        self.assertEqual(payload["count"], 0)


class SeedTests(DBTestCase):
    def test_seed_is_idempotent_and_covers_all_fixtures(self):
        first = refresh.seed_fixture_feeds(self.conn, FIXTURES)
        second = refresh.seed_fixture_feeds(self.conn, FIXTURES)
        self.assertEqual(len(first), 3)
        self.assertEqual(second, [])
        slugs = {f["fixture_slug"] for f in store.list_feeds(self.conn) if f["kind"] == "fixture"}
        self.assertEqual(slugs, {"tech-daily", "design-notes", "error-lab"})


if __name__ == "__main__":
    unittest.main()


class IdentityGuardTests(DBTestCase):
    """Weaker-identifier adoption must never merge distinct articles."""

    def _mk_feed(self) -> int:
        return store.create_feed(
            self.conn, url="https://guard.example.com/feed.xml", kind="http", topic="测试"
        )

    def _ingest(self, feed_id: int, items: str):
        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>G</title>'
            f"{items}</channel></rss>"
        )
        return store.ingest(self.conn, feed_id, parse_feed(xml.encode("utf-8")))

    def test_two_different_guids_sharing_a_link_stay_separate(self):
        feed_id = self._mk_feed()
        self._ingest(feed_id, """
          <item><title>甲</title><link>https://guard.example.com/blip</link>
            <guid>g-alpha</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>甲正文</p>]]></description></item>""")
        self._ingest(feed_id, """
          <item><title>乙</title><link>https://guard.example.com/blip</link>
            <guid>g-beta</guid><pubDate>Tue, 03 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>乙正文</p>]]></description></item>""")
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=10)
        self.assertEqual(len(rows), 2, "different guids must never be merged")
        self.assertEqual({r["identity_kind"] for r in rows}, {"guid"})

    def test_link_match_to_guid_row_requires_same_title(self):
        feed_id = self._mk_feed()
        self._ingest(feed_id, """
          <item><title>原始文章</title><link>https://guard.example.com/reused</link>
            <guid>g-original</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>原始正文</p>]]></description></item>""")
        entry_id = store.list_entries(self.conn, feed_id=feed_id, limit=10)[0]["id"]
        # The source stops emitting guids entirely and reuses the permalink
        # for a different post with a different title.
        stats = self._ingest(feed_id, """
          <item><title>完全不同的新文章</title><link>https://guard.example.com/reused</link>
            <pubDate>Tue, 03 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>新正文</p>]]></description></item>""")
        self.assertEqual(stats["new"], 1, "a repurposed link must not overwrite history")
        detail = store.get_entry(self.conn, entry_id)
        self.assertIn("原始正文", detail["content_html"])
        self.assertEqual(len(detail["revisions"]), 1)

    def test_link_match_to_guid_row_with_same_title_keeps_history(self):
        feed_id = self._mk_feed()
        self._ingest(feed_id, """
          <item><title>同一篇</title><link>https://guard.example.com/keep</link>
            <guid>g-keep</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>正文</p>]]></description></item>""")
        entry_id = store.list_entries(self.conn, feed_id=feed_id, limit=10)[0]["id"]
        stats = self._ingest(feed_id, """
          <item><title>同一篇</title><link>https://guard.example.com/keep</link>
            <pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>
            <description><![CDATA[<p>正文</p>]]></description></item>""")
        self.assertEqual(stats["new"], 0)
        self.assertEqual(store.get_entry(self.conn, entry_id)["identity_kind"], "guid")


class MissingFieldTests(DBTestCase):
    """A round that describes the same article less fully is not an edit."""

    def _mk_feed(self) -> int:
        return store.create_feed(
            self.conn, url="https://miss.example.com/feed.xml", kind="http", topic="测试"
        )

    def _ingest(self, feed_id: int, items: str):
        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>M</title>'
            f"{items}</channel></rss>"
        )
        return store.ingest(self.conn, feed_id, parse_feed(xml.encode("utf-8")))

    def test_omitted_title_does_not_create_a_revision(self):
        feed_id = self._mk_feed()
        with_title = (
            '<item><title>Alpha</title><link>https://miss.example.com/a</link>'
            '<guid>g</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>'
            '<description><![CDATA[<p>body</p>]]></description></item>'
        )
        without_title = (
            '<item><link>https://miss.example.com/a</link>'
            '<guid>g</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>'
            '<description><![CDATA[<p>body</p>]]></description></item>'
        )
        self._ingest(feed_id, with_title)
        entry_id = store.list_entries(self.conn, feed_id=feed_id, limit=10)[0]["id"]
        self.assertEqual(self._ingest(feed_id, without_title)["changed"], 0)
        self.assertEqual(self._ingest(feed_id, with_title)["changed"], 0)
        detail = store.get_entry(self.conn, entry_id)
        self.assertEqual(len(detail["revisions"]), 1)
        self.assertEqual(detail["title"], "Alpha")

    def test_empty_body_round_keeps_known_body(self):
        feed_id = self._mk_feed()
        full = (
            '<item><title>A</title><link>https://miss.example.com/a</link>'
            '<guid>g</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>'
            '<description><![CDATA[<p>real body</p>]]></description></item>'
        )
        empty = (
            '<item><title>A</title><link>https://miss.example.com/a</link>'
            '<guid>g</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>'
            '<description></description></item>'
        )
        self._ingest(feed_id, full)
        entry_id = store.list_entries(self.conn, feed_id=feed_id, limit=10)[0]["id"]
        stats = self._ingest(feed_id, empty)
        self.assertEqual(stats["changed"], 0)
        detail = store.get_entry(self.conn, entry_id)
        self.assertIn("real body", detail["content_html"])
        self.assertEqual(len(detail["revisions"]), 1)
        # The problem is still reported; we only refuse to erase the text.
        self.assertTrue(any(i["kind"] == "empty_body" and not i["resolved_at"]
                            for i in detail["issues"]))


class PublishedFallbackTests(DBTestCase):
    """A feed with only <updated> must not move "published" as it edits."""

    ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <title>T</title><link href="https://atom.example.com/"/>
      <entry><title>A</title><id>x</id><updated>{updated}</updated>
        <content type="html">&lt;p&gt;{body}&lt;/p&gt;</content></entry>
    </feed>"""

    def test_published_stays_at_first_seen_value(self):
        feed_id = store.create_feed(
            self.conn, url="https://atom.example.com/feed", kind="http", topic="测试"
        )
        store.ingest(self.conn, feed_id,
                     parse_feed(self.ATOM.format(updated="2026-10-01T00:00:00Z",
                                                 body="same").encode()))
        entry_id = store.list_entries(self.conn, feed_id=feed_id, limit=10)[0]["id"]
        store.ingest(self.conn, feed_id,
                     parse_feed(self.ATOM.format(updated="2026-11-02T00:00:00Z",
                                                 body="same").encode()))
        detail = store.get_entry(self.conn, entry_id)
        self.assertEqual(detail["published_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(detail["updated_at"], "2026-11-02T00:00:00Z")
        self.assertEqual(len(detail["revisions"]), 1)

    def test_idless_entry_with_moving_updated_is_not_duplicated(self):
        feed_id = store.create_feed(
            self.conn, url="https://atom.example.com/feed", kind="http", topic="测试"
        )
        xml = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>T</title><link href="https://atom.example.com/"/>
          <entry><title>A</title><updated>{updated}</updated>
            <content type="html">&lt;p&gt;same body&lt;/p&gt;</content></entry>
        </feed>"""
        store.ingest(self.conn, feed_id, parse_feed(xml.format(updated="2026-10-01T00:00:00Z").encode()))
        stats = store.ingest(self.conn, feed_id,
                             parse_feed(xml.format(updated="2026-11-02T00:00:00Z").encode()))
        self.assertEqual(stats["new"], 0, "moving <updated> must not fork the article")
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["missing_rounds"], 0)


class PromotionGuardTests(DBTestCase):
    """Promotion to a guid must not overwrite a different article."""

    def test_new_guid_reusing_an_old_link_with_a_new_date_stays_separate(self):
        feed_id = store.create_feed(
            self.conn, url="https://promo.example.com/feed", kind="http", topic="测试"
        )

        def ingest(items: str):
            xml = ('<?xml version="1.0"?><rss version="2.0"><channel><title>P</title>'
                   f"{items}</channel></rss>")
            return store.ingest(self.conn, feed_id, parse_feed(xml.encode()))

        ingest('<item><title>Old</title><link>https://promo.example.com/p</link>'
               '<pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>'
               '<description><![CDATA[<p>old body</p>]]></description></item>')
        old_id = store.list_entries(self.conn, feed_id=feed_id, limit=10)[0]["id"]

        stats = ingest('<item><title>Brand New</title><link>https://promo.example.com/p</link>'
                       '<guid>fresh-guid</guid><pubDate>Tue, 03 Nov 2026 01:00:00 GMT</pubDate>'
                       '<description><![CDATA[<p>new body</p>]]></description></item>')
        self.assertEqual(stats["new"], 1)
        old = store.get_entry(self.conn, old_id)
        self.assertEqual(old["title"], "Old")
        self.assertEqual(len(old["revisions"]), 1)
        self.assertIn("old body", old["content_html"])


class BadUrlTests(DBTestCase):
    """A syntactically impossible subscription URL is an error, never a crash."""

    def _refresh(self, url: str):
        feed_id = store.create_feed(self.conn, url=url, kind="http", topic="测试")
        return refresh.refresh_feed(self.conn, feed_id, fixtures=FIXTURES)

    def test_syntactically_invalid_urls_return_bad_url_without_retrying(self):
        for url in ("http://[::1/feed.xml", "http://exa mple.com/feed.xml", "not a url"):
            result = self._refresh(url)
            self.assertEqual(result["status"], "network_error", url)
            self.assertEqual(result["error_code"], "bad_url", url)
        feed = store.get_feed(self.conn, result["feed_id"])
        self.assertEqual(feed["last_result"], "failed")


class DegenerateIdentityTests(DBTestCase):
    """An entry with nothing but a title must never be merged by that title."""

    def _mk_feed(self) -> int:
        return store.create_feed(
            self.conn, url="https://degen.example.com/feed", kind="http", topic="测试"
        )

    def _ingest(self, feed_id: int, items: str):
        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>D</title>'
            f"<link>https://degen.example.com/</link>{items}</channel></rss>"
        )
        return store.ingest(self.conn, feed_id, parse_feed(xml.encode()))

    def test_titles_alone_never_identify(self):
        feed_id = self._mk_feed()
        stats = self._ingest(
            feed_id,
            '<item><title>Weekly Update</title><description></description></item>'
            '<item><title>Weekly Update</title><guid>g-x</guid>'
            '<description></description></item>',
        )
        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["unidentified"], 1)
        rows = store.list_entries(self.conn, feed_id=feed_id, limit=50)
        self.assertEqual(len(rows), 1, "the bodyless twin must not be merged in")
        self.assertEqual(rows[0]["identity_kind"], "guid")

    def test_unidentified_entries_do_not_grow_the_list_each_round(self):
        feed_id = self._mk_feed()
        item = '<item><title>Weekly Update</title><description></description></item>'
        for _ in range(4):
            self._ingest(feed_id, item)
        self.assertEqual(len(store.list_entries(self.conn, feed_id=feed_id, limit=50)), 0)
        issues = store.feed_detail(self.conn, feed_id)["issues"]
        self.assertEqual(len(issues), 1, "one report per offending entry, not one per round")

    def test_image_only_entries_with_different_images_stay_separate(self):
        feed_id = self._mk_feed()
        stats = self._ingest(
            feed_id,
            '<item><title>Photo</title><description><![CDATA['
            '<img src="https://degen.example.com/a.jpg">]]></description></item>'
            '<item><title>Photo</title><description><![CDATA['
            '<img src="https://degen.example.com/b.jpg">]]></description></item>',
        )
        self.assertEqual(stats["new"], 2)

    def test_shared_link_survives_one_guid_being_dropped(self):
        feed_id = self._mk_feed()
        both = (
            '<item><title>A</title><link>https://degen.example.com/shared</link>'
            '<guid>g-a</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>'
            '<description><![CDATA[<p>aaa</p>]]></description></item>'
            '<item><title>B</title><link>https://degen.example.com/shared</link>'
            '<guid>g-b</guid><pubDate>Tue, 03 Nov 2026 01:00:00 GMT</pubDate>'
            '<description><![CDATA[<p>bbb</p>]]></description></item>'
        )
        dropped = both.replace("<guid>g-b</guid>", "")
        self._ingest(feed_id, both)
        stats = self._ingest(feed_id, dropped)
        self.assertEqual(stats["new"], 0, "must not fork when one guid disappears")
        self.assertEqual(stats["missing"], 0)
        self.assertEqual(len(store.list_entries(self.conn, feed_id=feed_id, limit=50)), 2)


class TopicCountTests(DBTestCase):
    def test_paused_feeds_are_not_multiplied_by_their_entries(self):
        feed_id = store.create_feed(
            self.conn, url="https://count.example.com/feed", kind="http", topic="T"
        )
        items = "".join(
            f'<item><title>e{i}</title><link>https://count.example.com/{i}</link>'
            f'<guid>g{i}</guid><pubDate>Mon, 02 Nov 2026 01:00:00 GMT</pubDate>'
            f'<description><![CDATA[<p>b{i}</p>]]></description></item>'
            for i in range(3)
        )
        xml = ('<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
               f"{items}</channel></rss>")
        store.ingest(self.conn, feed_id, parse_feed(xml.encode()))
        refresh.set_paused(self.conn, feed_id, True)
        counts = store.topic_counts(self.conn)
        self.assertEqual(counts, [{"topic": "T", "entries": 3, "feeds": 1, "paused_feeds": 1}])


class SchedulerTests(DBTestCase):
    def test_run_once_refreshes_due_feeds_and_skips_paused(self):
        class FakeApp:
            def __init__(self, conn, fixtures):
                self.conn = conn
                self.fixtures = fixtures

            def connect(self):
                import sqlite3

                new = sqlite3.connect(self.conn.execute("PRAGMA database_list").fetchone()[2])
                new.row_factory = sqlite3.Row
                new.execute("PRAGMA foreign_keys = ON")
                return new

        good = self.add_fixture_feed("tech-daily", "v1")
        paused = self.add_fixture_feed("design-notes", "v1")
        refresh.set_paused(self.conn, paused, True)
        scheduler = refresh.Scheduler(FakeApp(self.conn, FIXTURES), interval=0.05)
        results = scheduler.run_once()
        by_feed = {r["feed_id"]: r["status"] for r in results}
        self.assertEqual(by_feed[good], "ok")
        self.assertEqual(by_feed[paused], "paused")
        self.assertIsNotNone(scheduler.last_run)

        # A second run must be a conditional no-op for the good feed.
        second = {r["feed_id"]: r["status"] for r in scheduler.run_once()}
        self.assertEqual(second[good], "not_modified")

    def test_scheduler_reads_due_time_from_backoff(self):
        class FakeApp:
            def __init__(self, conn):
                self.conn = conn
                self.fixtures = FIXTURES

            def connect(self):
                import sqlite3

                new = sqlite3.connect(self.conn.execute("PRAGMA database_list").fetchone()[2])
                new.row_factory = sqlite3.Row
                new.execute("PRAGMA foreign_keys = ON")
                return new

        feed_id = self.add_fixture_feed("error-lab", "v500")
        self.refresh(feed_id)
        scheduler = refresh.Scheduler(FakeApp(self.conn), interval=0.05)
        results = scheduler.run_once()
        self.assertEqual(results[0]["status"], "retry_scheduled")


class IssueResolutionTests(DBTestCase):
    def test_success_resolves_all_source_level_issue_kinds(self):
        feed_id = self.add_fixture_feed("error-lab", "v500")
        self.refresh(feed_id)
        self.assertEqual(store.get_feed(self.conn, feed_id)["last_result"], "failed")
        kind = f"http_500"
        self.assertTrue(
            self.conn.execute(
                "SELECT 1 FROM entry_issues WHERE feed_id = ? AND kind = ? AND resolved_at IS NULL",
                (feed_id, kind),
            ).fetchone()
        )
        # The same source now serves a valid document.
        store.update_feed_fields(self.conn, feed_id, fixture_version="v3")
        result = self.refresh(feed_id, version="v3")
        self.assertEqual(result["status"], "ok")
        still_open = self.conn.execute(
            "SELECT kind FROM entry_issues WHERE feed_id = ? AND identity_key = '' "
            "AND resolved_at IS NULL",
            (feed_id,),
        ).fetchall()
        self.assertEqual([r["kind"] for r in still_open], [])

    def test_failure_count_resets_after_success(self):
        feed_id = self.add_fixture_feed("error-lab", "v404")
        self.refresh(feed_id)
        self.assertEqual(store.get_feed(self.conn, feed_id)["failure_count"], 1)
        self.refresh(feed_id, version="v3")
        feed = store.get_feed(self.conn, feed_id)
        self.assertEqual(feed["failure_count"], 0)
        self.assertIsNone(feed["next_retry_at"])


class FeedVarietyTests(DBTestCase):
    """Real-world feed shapes that differ from the bundled fixtures."""

    def _feed(self, xml: str):
        return parse_feed(xml.encode("utf-8"))

    def test_content_encoded_preferred_over_description(self):
        feed = self._feed("""<?xml version="1.0"?>
        <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel><title>X</title>
            <item><title>T</title><link>https://x.example/1</link>
              <description>摘要</description>
              <content:encoded><![CDATA[<p>完整正文第一段</p><p>第二段</p>]]></content:encoded>
            </item></channel></rss>""")
        self.assertIn("完整正文第一段", feed.entries[0].content_html)
        self.assertIn("第二段", feed.entries[0].content_html)

    def test_relative_links_resolve_against_feed_base(self):
        feed = self._feed("""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>A</title><link href="https://a.example/feed" rel="self"/>
          <id>urn:a</id><updated>2026-11-02T01:00:00Z</updated>
          <entry><title>e</title><link href="/posts/7" rel="alternate"/>
            <id>tag:a,2026:7</id><updated>2026-11-02T01:00:00Z</updated>
            <content type="html">&lt;p&gt;正文&lt;/p&gt;</content></entry>
        </feed>""")
        self.assertEqual(feed.entries[0].link, "https://a.example/posts/7")

    def test_atom_requires_updated_only(self):
        feed = self._feed("""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
          <id>urn:a</id><updated>2026-11-02T01:00:00Z</updated>
          <entry><title>e</title><id>tag:a,2026:1</id>
            <updated>2026-10-01T03:00:00Z</updated>
            <content type="html">&lt;p&gt;正文&lt;/p&gt;</content></entry></feed>""")
        entry = feed.entries[0]
        self.assertEqual(entry.published, entry.updated)
        self.assertEqual(entry.published.day, 1)
        self.assertFalse([i for i in entry.issues if i["code"] == "bad_date"])

    def test_rss_date_with_numeric_zone(self):
        value, error = parse_date("Tue, 03 Nov 2026 03:15:00 +0800")
        self.assertIsNone(error)
        self.assertEqual(value.hour, 19)  # previous day in UTC
        self.assertEqual(value.day, 2)

    def test_empty_document_is_a_parse_error(self):
        with self.assertRaises(FeedParseError):
            parse_feed(b"")

    def test_html_only_root_is_rejected(self):
        with self.assertRaises(FeedParseError):
            parse_feed(b"<!DOCTYPE html><html><body>not a feed</body></html>")

    def test_duplicate_guid_in_one_document_collapses(self):
        feed = self._feed("""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>D</title>
          <item><title>一</title><guid>same</guid><description>x</description></item>
          <item><title>二</title><guid>same</guid><description>y</description></item>
        </channel></rss>""")
        self.assertEqual(len(feed.entries), 1)


class IssueLabelTests(unittest.TestCase):
    def test_http_status_labels(self):
        self.assertEqual(store.issue_label("http_404"), "源返回 HTTP 404")
        self.assertEqual(store.issue_label("http_500"), "源返回 HTTP 500")

    def test_known_and_unknown(self):
        self.assertEqual(store.issue_label("bad_date"), "异常日期")
        self.assertEqual(store.issue_label("weird_kind"), "weird_kind")
        self.assertEqual(store.issue_label(""), "错误")


class DateSanityTests(unittest.TestCase):
    def test_two_digit_year_rewriting_is_rejected(self):
        # email.utils would silently read "0001" as 2001.
        self.assertEqual(parse_date("Mon, 01 Jan 0001 00:00:00 GMT")[0], None)
        self.assertEqual(parse_date("Sat, 01 Jan 0000 00:00:00 GMT")[0], None)

    def test_normal_years_still_parse(self):
        self.assertEqual(parse_date("Wed, 19 Jun 2019 08:00:00 GMT")[0].year, 2019)
        self.assertEqual(parse_date("Fri, 01 Jan 2100 00:00:00 GMT")[0].year, 2100)

    def test_numeric_offset_applied(self):
        value, error = parse_date("2026-11-02T09:30:00+14:00")
        self.assertIsNone(error)
        self.assertEqual((value.day, value.month, value.hour), (1, 11, 19))


class IssuePersistenceTests(DBTestCase):
    def test_issue_on_absent_entry_stays_open(self):
        feed_id = self.add_fixture_feed("error-lab", "v3")
        self.refresh(feed_id)
        bad = [r for r in store.list_entries(self.conn, feed_id=feed_id, limit=100)
               if r["title"] == "日期写成中文的条目"][0]
        # A failed fetch teaches us nothing about entries, so the bad-date
        # issue must stay open rather than be silently "resolved".
        store.update_feed_fields(self.conn, feed_id, fixture_version="v500")
        self.refresh(feed_id, version="v500")
        detail = store.get_entry(self.conn, bad["id"])
        open_issues = [i for i in detail["issues"] if not i["resolved_at"]]
        self.assertTrue(any(i["kind"] == "bad_date" for i in open_issues))

    def test_304_after_failure_resolves_source_issue(self):
        import json
        import shutil

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "fixtures"
        shutil.copytree(ROOT / "fixtures" / "tech-daily", root / "tech-daily")
        registry = FixtureRegistry(root)
        manifest_path = root / "tech-daily" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["versions"].append(
            {
                "id": "v500",
                "label": "v500",
                "file": None,
                "etag": None,
                "last_modified": None,
                "status": 500,
                "notes": "server error",
            }
        )
        manifest_path.write_text(json.dumps(manifest))

        feed_id = store.create_feed(
            self.conn,
            url=fixture_url("tech-daily", "v1"),
            kind="fixture",
            fixture_slug="tech-daily",
            fixture_version="v1",
            topic="测试",
            title="技术日报",
        )
        refresh.refresh_feed(self.conn, feed_id, fixtures=registry, force=True)

        # The source fails transiently: a source-level issue is opened.
        refresh.refresh_feed(self.conn, feed_id, fixtures=registry, version="v500", force=True)
        detail = store.feed_detail(self.conn, feed_id)
        open_src = [i for i in detail["issues"]
                    if not i["resolved_at"] and i["identity_key"] == ""]
        self.assertTrue(any("500" in i["kind"] for i in open_src), detail["issues"])

        # Back to the version whose ETag we stored: the reply is 304, which
        # proves the source is healthy again.
        store.update_feed_fields(self.conn, feed_id, fixture_version="v1")
        outcome = refresh.refresh_feed(self.conn, feed_id, fixtures=registry, force=False)
        self.assertEqual(outcome["status"], "not_modified")
        detail = store.feed_detail(self.conn, feed_id)
        still_open = [i for i in detail["issues"]
                      if not i["resolved_at"] and i["identity_key"] == ""]
        self.assertEqual(still_open, [])

    def test_issue_on_entry_dropped_from_feed_stays_open(self):
        feed_id = self.add_fixture_feed("error-lab", "v2")
        self.refresh(feed_id)
        bad = [r for r in store.list_entries(self.conn, feed_id=feed_id, limit=100)
               if r["title"] == "只有日期没有时间的条目"][0]
        # v3 is a valid document that no longer contains that entry.
        self.refresh(feed_id, version="v3")
        detail = store.get_entry(self.conn, bad["id"])
        self.assertGreater(detail["missing_rounds"], 0)
        open_issues = [i for i in detail["issues"] if not i["resolved_at"]]
        self.assertTrue(any(i["kind"] == "bad_date" for i in open_issues),
                        "absent entry was not re-parsed, so its issue is unresolved")

    def test_issue_resolves_when_entry_is_reparsed_clean(self):
        feed_id = self.add_fixture_feed("tech-daily", "v3")
        self.refresh(feed_id)
        linked = [r for r in store.list_entries(self.conn, feed_id=feed_id, limit=100)
                  if r["identity_kind"] == "link"][0]
        issues = [i for i in store.get_entry(self.conn, linked["id"])["issues"]
                  if not i["resolved_at"]]
        self.assertTrue(any(i["kind"] == "missing_identity" for i in issues))

        # The same source starts supplying a guid for that article.
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>技术日报</title>
          <item><title>灰度发布检查清单</title>
            <link>https://tech.example.com/2026/11/canary-checklist.html</link>
            <guid>tech-2026-e</guid>
            <pubDate>Thu, 05 Nov 2026 02:00:00 GMT</pubDate>
            <description><![CDATA[<p>同一篇正文。</p>]]></description></item>
        </channel></rss>"""
        store.ingest(self.conn, feed_id, parse_feed(xml.encode()), source_version="inline")
        detail = store.get_entry(self.conn, linked["id"])
        self.assertEqual(len(store.list_entries(self.conn, feed_id=feed_id, limit=1000)), 6)
        self.assertEqual(detail["identity_kind"], "guid")
        issues = [i for i in detail["issues"] if not i["resolved_at"]]
        self.assertFalse(any(i["kind"] == "missing_identity" for i in issues))
