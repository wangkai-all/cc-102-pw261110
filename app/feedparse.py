"""Feed parsing and normalisation.

The parser understands both RSS 2.0 and Atom 1.0 and returns a list of
plain dictionaries.  It never raises for malformed *entries*: a bad entry
(no body, impossible date) is reported as an issue attached to the entry so
the caller can decide what to do with it.  Malformed documents raise
``FeedParseError``.
"""

from __future__ import annotations

import email.utils
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Errors


class FeedParseError(Exception):
    """The document as a whole cannot be read."""


# ---------------------------------------------------------------------------
# Namespaces.  We match on local names so that slightly wrong/absent
# namespaces in real feeds do not break readability.


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _first(parent: ET.Element, *names: str) -> ET.Element | None:
    for child in parent:
        if _local(child.tag) in names:
            return child
    return None


def _all(parent: ET.Element, *names: str) -> list[ET.Element]:
    return [c for c in parent if _local(c.tag) in names]


def _attr(el: ET.Element | None, *names: str) -> str:
    if el is None:
        return ""
    for key, value in el.attrib.items():
        if _local(key) in names:
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Dates

# Date strings we refuse to guess about.  A bare day such as ``2024-05-01``
# is ambiguous and is treated as an error rather than assumed to be UTC.
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"\s*([Zz]|[+-]\d{2}:?\d{2})?$"
)
_TWO_DIGIT_YEAR_RE = re.compile(
    r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2}\b", re.I
)
_MONTH_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b", re.I
)

# ---------------------------------------------------------------------------


@dataclass
class ParsedEntry:
    title: str
    link: str
    guid: str
    published: datetime | None
    updated: datetime | None
    content_html: str
    summary: str
    issues: list[dict[str, str]] = field(default_factory=list)
    # True when the source had no publication date and we used <updated> in
    # its place.  The store then keeps the first such value it saw instead of
    # letting a later <updated> drag the "published" date forwards.
    published_is_fallback: bool = False


@dataclass
class ParsedFeed:
    title: str
    link: str
    entries: list[ParsedEntry] = field(default_factory=list)


def parse_date(raw: str | None) -> tuple[datetime | None, str | None]:
    """Parse an RSS/Atom date.

    Returns ``(datetime, None)`` on success and ``(None, reason)`` on
    failure.  All results are timezone-aware UTC.  We deliberately do not
    fall back to "now" for unparseable values: silently substituting today
    is exactly the bug that makes an old republished article look new.
    """
    if raw is None or not raw.strip():
        return None, "missing"
    value = raw.strip()

    match = _ISO_RE.match(value)
    if match:
        year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
        second = int(match.group(6) or 0)
        tzpart = (match.group(7) or "Z").upper()
        try:
            base = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except ValueError:
            return None, "date out of range"
        if tzpart == "Z":
            offset = 0
        else:
            sign = 1 if tzpart[0] == "+" else -1
            digits = tzpart[1:].replace(":", "")
            offset = sign * (int(digits[:2]) * 60 + int(digits[2:]))
        try:
            return base - timedelta(minutes=offset), None
        except OverflowError:
            # "9999-12-31T23:59:59-05:00" converts past datetime.max.  The
            # value is syntactically fine but not representable; reporting it
            # as bad keeps one corrupt entry from aborting the document.
            return None, "date out of range"

    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return None, "date without a time is ambiguous"

    if _MONTH_RE.search(value):
        # Look for a four-digit year in the date portion only.  Searching the
        # whole string would pick up a "+0800" offset when the year itself was
        # written with two digits.
        date_part = value.split(":", 1)[0]
        year_match = re.search(r"\b(\d{4})\b", date_part)
        if year_match:
            year = int(year_match.group(1))
            # email.utils follows RFC 2822's two-digit-year rule and will
            # happily turn "0001" into 2001.  A feed date outside this window
            # is far more likely to be corrupt than genuine, and letting it
            # through would move an old article to a plausible modern date.
            if year < 1970 or year > 2200:
                return None, f"implausible year {year}"
        elif _TWO_DIGIT_YEAR_RE.search(date_part):
            # "02 Nov 26" is a two-digit year, which email.utils would silently
            # read as 2026.  Same ambiguity rule as a date with no time: we
            # refuse to guess rather than risk moving an old article forward.
            return None, "two-digit year is ambiguous"
        # parsedate_tz also accepts a full weekday name, a trailing comment
        # and named zones such as "UTC", all of which occur in real feeds.
        parsed = email.utils.parsedate_tz(value)
        if parsed is None:
            return None, "unparseable date"
        try:
            base = datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None, "unparseable date"
        offset = parsed[9]
        if offset is None or offset == -1:  # "-0000": UTC, zone unknown
            offset = 0
        try:
            return base - timedelta(seconds=offset), None
        except OverflowError:
            return None, "date out of range"

    return None, "unparseable date"


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# URL normalisation
#
# Links are only a fallback identity, so we normalise conservatively and
# document every step.  We do NOT drop query parameters or case-fold the
# path: both can distinguish two genuinely different articles.

_INVALID_HOST_RE = re.compile(r'[\s<>{}|\\^"`]')


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer", "utm_social",
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "ref", "ref_src", "ref_url", "source", "sourceid",
    "_hsenc", "_hsmi", "mkt_tok", "campaign", "cid",
}


def normalize_url(raw: str, base: str | None = None) -> str:
    """Normalise a link for use as an identity fallback.

    Rules, applied in order:
      1. Resolve relative links against the feed link.
      2. Lowercase scheme and host; strip a default port.
      3. Drop a trailing ``#fragment``.
      4. Drop known campaign/tracking query parameters only.
      5. Sort the remaining query parameters.
      6. Remove a trailing slash from the path (but keep a bare "/").
    The path is otherwise preserved byte-for-byte.
    """
    if not raw:
        return ""
    value = raw.strip()
    # ``urljoin``/``urlsplit`` raise on a hand-edited port or a broken IPv6
    # literal, and one such link must not abort the whole document.  Returning
    # "" makes the caller fall back to a content fingerprint instead.
    try:
        if base:
            value = urljoin(base, value)
        parts = urlsplit(value)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        return ""
    # Only http(s) links are usable as an article identity and as something
    # the reader can click.  A ``javascript:`` or ``data:`` "link" is never
    # a permalink, so treating it as one would both pollute identity and
    # hand the UI an unsafe href.  Relative links were already resolved
    # against ``base`` above, so a scheme-less result here means the feed
    # itself gave us no usable base.
    if parts.scheme.lower() not in ("http", "https") or not hostname:
        return ""
    # A host with a space or a delimiter is not a host at all; urlsplit passes
    # it through, but urllib then raises.  Rejecting it here keeps a bad
    # subscription from being accepted and from aborting a document parse.
    if _INVALID_HOST_RE.search(hostname):
        return ""
    scheme = parts.scheme.lower()
    host = hostname.lower().rstrip(".")
    if ":" in host:
        # ``hostname`` strips the brackets from an IPv6 literal; put them back
        # or the resulting URL would be unparseable.
        host = f"[{host}]"
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    if parts.username:
        auth = parts.username
        if parts.password:
            auth += ":" + parts.password
        netloc = f"{auth}@{netloc}"

    query_pairs = []
    for chunk in parts.query.split("&"):
        if not chunk:
            continue
        key = chunk.split("=", 1)[0]
        if key.lower() in _TRACKING_PARAMS:
            continue
        query_pairs.append(chunk)
    query_pairs.sort()

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, "&".join(query_pairs), ""))


# ---------------------------------------------------------------------------
# Content


def strip_html(fragment: str) -> str:
    """Produce plain text for the "empty body" check and search."""
    if not fragment:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------


def parse_feed(raw: bytes, *, base_url: str | None = None) -> ParsedFeed:
    if not raw or not raw.strip():
        raise FeedParseError("the response body is empty")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FeedParseError(f"malformed XML: {exc}") from exc

    tag = _local(root.tag)
    if tag == "feed":
        return _parse_atom(root, base_url)
    if tag in ("rss", "RDF"):
        return _parse_rss(root, base_url)
    raise FeedParseError(f"unrecognised feed root element <{tag}>")


def _feed_link(container: ET.Element, base_url: str | None) -> str:
    # Prefer the human-facing alternate link, then the self link, then the
    # plain-text RSS channel link.  We avoid the Atom <id> unless nothing else
    # exists: ids are frequently opaque URNs which are useless as a base for
    # relative entry links.
    alternate = ""
    self_link = ""
    any_href = ""
    for child in container:
        if _local(child.tag) != "link":
            continue
        href = _attr(child, "href")
        if not href:
            continue
        rel = _attr(child, "rel") or "alternate"
        if rel == "alternate" and not alternate:
            alternate = href
        elif rel == "self" and not self_link:
            self_link = href
        if not any_href:
            any_href = href
    plain = _first(container, "link")
    plain_text = _text(plain) if plain is not None else ""
    chosen = alternate or plain_text or self_link or any_href
    if chosen:
        # A channel link is only useful as a base for relative entry links,
        # and it must not be able to crash the parse (``urljoin`` raises on a
        # broken IPv6 literal) nor be a non-web URL that then becomes the
        # base for every entry.
        try:
            joined = urljoin(base_url or "", chosen)
            parts = urlsplit(joined)
        except ValueError:
            joined, parts = "", None
        if parts is not None and parts.scheme.lower() in ("http", "https") and parts.hostname:
            return joined
    for child in container:
        if _local(child.tag) == "id" and _text(child):
            return _text(child)
    return base_url or ""


def _parse_rss(root: ET.Element, base_url: str | None) -> ParsedFeed:
    channel = _first(root, "channel")
    if channel is None:
        # RDF (RSS 1.0) puts items directly under the root.
        channel = root
    feed_title = _text(_first(channel, "title")) or _text(_first(root, "title"))
    feed_link = _feed_link(channel, base_url)
    items = _all(channel, "item")
    if not items:  # RSS 1.0 may nest items beside channel in RDF
        items = [el for el in root if _local(el.tag) == "item"]

    entries: list[ParsedEntry] = []
    seen_guids: set[str] = set()
    for item in items:
        title = _text(_first(item, "title"))
        raw_link = _text(_first(item, "link"))
        guid_el = _first(item, "guid")
        guid = _text(guid_el)
        # An RSS guid may be a non-URL key; only URL-ish guids make usable links.
        if not raw_link and guid and "://" in guid:
            raw_link = guid
        link = _attr(_first(item, "link"), "href") or raw_link
        link = normalize_url(link, base_url or feed_link) if link else ""

        def date_from(*names: str) -> tuple[datetime | None, str | None]:
            for name in names:
                el = _first(item, name)
                if el is not None and _text(el):
                    return parse_date(_text(el))
            return None, "missing"

        published, pub_issue = date_from("pubDate", "published", "issued", "date")
        updated, upd_issue = date_from("lastBuildDate", "updated", "modified")
        published_is_fallback = False
        if published is None and updated is not None:
            # Atom only requires <updated>; when there is no separate
            # publication date the first update is the best evidence we
            # have.  Record it as published but keep updated too.  A *bad*
            # published value keeps its issue: the fallback keeps the article
            # readable, but the feed's own field is still broken and the
            # reader deserves to see that.
            published = updated
            published_is_fallback = True

        # Prefer full-text payloads over the short description.  Order matters:
        # content:encoded > content (Atom-style in RSS) > description.
        content_html = ""
        for name in ("encoded", "content", "description"):
            el = _first(item, name)
            if el is not None and _text(el):
                content_html = _text(el)
                break
        summary = _text(_first(item, "summary", "description"))
        if not summary and content_html:
            summary = strip_html(content_html)[:280]

        issues: list[dict[str, str]] = []
        if pub_issue and pub_issue != "missing":
            issues.append({"code": "bad_date", "field": "published", "detail": pub_issue})
        if upd_issue and upd_issue != "missing":
            issues.append({"code": "bad_date", "field": "updated", "detail": upd_issue})
        if not strip_html(content_html):
            issues.append({"code": "empty_body", "detail": "entry has no body content"})

        if guid:
            if guid in seen_guids:
                continue
            seen_guids.add(guid)

        entries.append(
            ParsedEntry(
                title=title,
                link=link,
                guid=guid,
                published=published,
                updated=updated,
                content_html=content_html,
                summary=summary,
                issues=issues,
                published_is_fallback=published_is_fallback,
            )
        )

    return ParsedFeed(title=feed_title, link=feed_link, entries=entries)


def _parse_atom(root: ET.Element, base_url: str | None) -> ParsedFeed:
    feed_title = _text(_first(root, "title"))
    feed_link = _feed_link(root, base_url)
    xml_base = _attr(root, "base") or base_url
    entries: list[ParsedEntry] = []
    seen_ids: set[str] = set()
    for entry in _all(root, "entry"):
        title = _text(_first(entry, "title"))
        link = ""
        for child in entry:
            if _local(child.tag) == "link":
                rel = _attr(child, "rel") or "alternate"
                if rel == "alternate" and _attr(child, "href"):
                    link = _attr(child, "href")
                    break
        if not link:
            plain = _first(entry, "link")
            if plain is not None:
                link = _attr(plain, "href")
        link = normalize_url(link, xml_base or feed_link) if link else ""

        guid = _text(_first(entry, "id"))

        published, pub_issue = parse_date(_text(_first(entry, "published", "issued")))
        updated, upd_issue = parse_date(_text(_first(entry, "updated", "modified")))
        published_is_fallback = False
        if published is None and updated is not None:
            published = updated
            published_is_fallback = True

        content_html = ""
        # <content> may carry a type; text/html is what we render.
        for child in entry:
            if _local(child.tag) == "content" and _text(child):
                content_html = _text(child)
                break
        if not content_html:
            content_html = _text(_first(entry, "summary"))
        summary = _text(_first(entry, "summary")) or strip_html(content_html)[:280]

        issues: list[dict[str, str]] = []
        if pub_issue and pub_issue != "missing":
            issues.append({"code": "bad_date", "field": "published", "detail": pub_issue})
        if upd_issue and upd_issue != "missing":
            issues.append({"code": "bad_date", "field": "updated", "detail": upd_issue})
        if not strip_html(content_html):
            issues.append({"code": "empty_body", "detail": "entry has no body content"})

        if guid:
            if guid in seen_ids:
                continue
            seen_ids.add(guid)

        entries.append(
            ParsedEntry(
                title=title,
                link=link,
                guid=guid,
                published=published,
                updated=updated,
                content_html=content_html,
                summary=summary,
                issues=issues,
                published_is_fallback=published_is_fallback,
            )
        )
    return ParsedFeed(title=feed_title, link=feed_link, entries=entries)
