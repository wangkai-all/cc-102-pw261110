"""A small allowlist HTML sanitiser for untrusted feed bodies.

Feed content is rendered in the reading pane and in the diff view, so it must
not be able to run scripts or load arbitrary remote resources.  We keep the
ordinary text-level markup a feed actually uses and drop everything else.
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span", "strong", "b", "em", "i", "u", "s",
    "del", "ins", "mark", "small", "sub", "sup", "code", "pre", "blockquote",
    "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img", "figure", "figcaption", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "caption", "cite", "q", "time", "abbr",
}
VOID_TAGS = {"br", "hr", "img"}
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
    "time": {"datetime"},
    "abbr": {"title"},
}
SAFE_SCHEMES = ("http://", "https://", "mailto:", "/", "#", "data:image/")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self._skip_depth = 0

    def _safe_url(self, value: str) -> str:
        value = value.strip()
        lowered = value.lower()
        if any(lowered.startswith(scheme) for scheme in SAFE_SCHEMES):
            return value
        return ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed", "form", "input", "link", "meta"):
            self._skip_depth += 1
            return
        if self._skip_depth or tag not in ALLOWED_TAGS:
            return
        parts = [tag]
        allowed = ALLOWED_ATTRS.get(tag, set())
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name not in allowed:
                continue
            if name in ("href", "src"):
                value = self._safe_url(value or "")
                if not value:
                    continue
            parts.append(f'{name}="{escape(value or "", quote=True)}"')
        self.out.append("<" + " ".join(parts) + ">")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        tag = tag.lower()
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS and self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed", "form", "input", "link", "meta"):
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth or tag not in ALLOWED_TAGS:
            return
        if tag in self.stack:
            # Close any unclosed children first so output stays well formed.
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(fragment: str) -> str:
    if not fragment:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient, belt and braces
        return escape(fragment)
    return parser.result()
