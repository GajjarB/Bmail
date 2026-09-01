"""HTML to text.

Getting this wrong poisons everything downstream, so it is deliberately boring:
drop script/style/head, keep block structure as newlines, unescape entities.
`selectolax` is used when installed because it is far faster and handles broken
markup better; the stdlib parser is a complete fallback, not a stub.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

_DROP_TAGS = {"script", "style", "head", "title", "noscript", "svg", "template"}
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "section", "article", "header", "footer", "blockquote", "pre",
}

try:  # pragma: no cover - import guard
    from selectolax.parser import HTMLParser as _Selectolax
except Exception:  # pragma: no cover
    _Selectolax = None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip or not data.strip():
            return
        # Separate adjacent inline runs, so "Hello<span>world</span>" does not
        # become one fused token. Matches what the selectolax path produces.
        if self._parts and not self._parts[-1][-1:].isspace() and not data[:1].isspace():
            self._parts.append(" ")
        self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


# Block boundaries are turned into newlines *before* parsing on the selectolax
# path. Asking it for a "\n" separator instead would also break between inline
# elements, turning "Hallo <b>Welt</b>" into two lines and splitting words apart
# for search and classification.
_BLOCK_RE = re.compile(
    r"<\s*/?\s*(?:p|div|br|tr|li|h[1-6]|table|section|article|header|footer"
    r"|blockquote|pre|ul|ol)\b[^>]*>",
    re.IGNORECASE,
)


def _collapse(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(html: str) -> str:
    """Return readable plain text for an HTML mail part."""
    if not html:
        return ""
    if _Selectolax is not None:
        tree = _Selectolax(_BLOCK_RE.sub("\n", html))
        for tag in _DROP_TAGS:
            for node in tree.css(tag):
                node.decompose()
        body = tree.body or tree
        return _collapse(body.text(separator=" "))
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return _collapse(unescape(re.sub(r"<[^>]+>", " ", html)))
    return _collapse(parser.text)
