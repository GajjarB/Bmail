"""HTML to readable text.

Getting this wrong poisons everything downstream: the classifier reads the
first few hundred characters, the rules quote a line as evidence, and the app
shows the result to a person. So the text should read the way the mail looks
in a mail client, not the way its markup happens to be nested:

* what a mail client hides stays hidden: script, style and head, and elements
  styled `display:none` inline or by a plain class rule in the head. That is
  where newsletters and applicant-tracking systems put the "preheader", often
  padded with a hundred invisible spacer characters;
* invisible characters (zero-width joiners, soft hyphens, grapheme joiners)
  are dropped, because they only exist to pad that preheader;
* layout survives as plain text: paragraphs as blank lines, list items as
  bullets, table cells side by side ("Date · Tuesday"), rows as lines;
* inline markup does not split words: `<b>B</b>ewerbung` is one word, as it is
  on screen.

`selectolax` is used when installed because it parses faster and repairs broken
markup the way a browser does; the stdlib parser is a complete fallback, not a
stub. Both feed the same writer, so they produce the same text.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

try:  # pragma: no cover - import guard
    # Lexbor, not the older Modest engine: Lexbor is Apache-2.0 and maintained;
    # Modest is LGPL-2.1 and no longer developed.
    from selectolax.lexbor import LexborHTMLParser as _Selectolax
except Exception:  # pragma: no cover
    _Selectolax = None

# Never shown by a mail client.
_DROP_TAGS = {"script", "style", "head", "title", "noscript", "svg", "template", "object"}
_VOID_TAGS = {
    "br", "hr", "img", "wbr", "meta", "link", "input", "col", "area", "base",
    "source", "embed", "track", "param",
}
# Dropped because they draw rather than say; read through when they turn out
# to hold the message (see _Writer.swallowed).
_EMBEDDED_TAGS = {"svg", "object", "template"}
# What a head may hold. Anything else starting inside one means the head is
# over, said or not: mail that never writes </head> is common, and reading it
# as "still in the head" throws the whole message away.
_HEAD_TAGS = {"head", "title", "meta", "link", "style", "script", "base", "noscript", "template"}
# A blank line before and after.
_PARAGRAPH_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol", "dl"}
# A line of their own.
_LINE_TAGS = {
    "div", "tr", "li", "section", "article", "header", "footer", "main", "nav",
    "aside", "address", "center", "figure", "figcaption", "form", "fieldset",
    "caption", "details", "summary", "dt", "dd", "tbody", "thead", "tfoot",
}
_CELL_TAGS = {"td", "th"}
CELL_SEPARATOR = " · "

# An element that is still open when one of these starts has ended, in the
# stdlib path, which does not build a tree. Each maps to the elements that
# bound the search: a <li> closes an open <li>, but not one in an outer list.
_IMPLIED_END = {
    "li": ({"li"}, {"ul", "ol"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "tr": ({"tr", "td", "th"}, {"table"}),
    "p": ({"p"}, {"div", "td", "th", "li", "blockquote", "table"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
}

# Characters that take no space on screen and exist in mail only to pad a
# preheader. U+200D stays: emoji sequences are built with it.
_INVISIBLE = dict.fromkeys(map(ord, "­͏᠎​‌⁠⁡⁢⁣﻿"))

_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", re.I)
_ZERO_BOX = re.compile(r"(?:^|;)\s*(?:max-)?height\s*:\s*0(?:px)?\s*(?:!important\s*)?(?:;|$)", re.I)
_OVERFLOW_HIDDEN = re.compile(r"(?:^|;)\s*overflow\s*:\s*hidden\b", re.I)

# Class rules in the head that hide something everywhere. Rules inside @media
# hide things only on some screens (the mobile copy of a desktop block, say),
# so those blocks are cut out first.
_MEDIA_BLOCK = re.compile(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", re.I)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CLASS_ONLY = re.compile(r"^\s*(?:[a-z0-9]*\.)?\.?([\w-]+)\s*$", re.I)


def hidden_classes(stylesheets: list[str]) -> set[str]:
    """Class names that a head stylesheet hides on every screen."""
    hidden: set[str] = set()
    for sheet in stylesheets:
        sheet = _MEDIA_BLOCK.sub(" ", _CSS_COMMENT.sub(" ", sheet))
        for selectors, body in _CSS_RULE.findall(sheet):
            if not _HIDDEN_STYLE.search(";" + body):
                continue
            for selector in selectors.split(","):
                selector = selector.strip()
                # Only a bare class, or tag.class: anything more specific may
                # hide it only in a context this writer does not track.
                if "." in selector and _CLASS_ONLY.match(selector):
                    hidden.add(selector.rsplit(".", 1)[1].lower())
    return hidden


def _is_hidden(attrs: dict[str, str | None], classes: set[str]) -> bool:
    if "hidden" in attrs:
        return True
    style = attrs.get("style") or ""
    if style and (
        _HIDDEN_STYLE.search(style)
        or (_ZERO_BOX.search(style) and _OVERFLOW_HIDDEN.search(style))
    ):
        return True
    if classes and attrs.get("class"):
        return any(name.lower() in classes for name in (attrs["class"] or "").split())
    return False


class _Writer:
    """Turns start/end/text events into readable text."""

    def __init__(self, classes: set[str], honour_hidden: bool = True,
                 transparent: frozenset[str] = frozenset()) -> None:
        self.classes = classes
        self.honour_hidden = honour_hidden
        self.transparent = transparent            # dropped tags to read through instead
        self.parts: list[str] = []
        self.open: list[tuple[str, bool]] = []   # (tag, skips its content)
        self.skipping = 0
        self.lists: list[list[int | str]] = []   # [kind, counter]
        self.pre = 0
        self.space = False                        # whitespace seen since the last word
        self.lead = ""                            # bullet or cell separator owed to the next word
        self.hidden_chars = 0
        self.embedded: dict[str, int] = {}        # text inside each kind of embedded element
        self.shown_chars = 0

    # --- layout -------------------------------------------------------------

    def _at_line_start(self) -> bool:
        return not self.parts or self.parts[-1].endswith("\n")

    def _break(self, lines: int) -> None:
        # A bullet waits for its item's first word, even across the <p> that
        # Outlook wraps it in; a cell separator never crosses a line.
        if self.lead == CELL_SEPARATOR:
            self.lead = ""
        self.space = False
        if not self.parts:
            return
        tail = "".join(self.parts[-3:])
        have = len(tail) - len(tail.rstrip("\n"))
        if have < lines:
            self.parts.append("\n" * (lines - have))

    def _word(self, word: str) -> None:
        if self._at_line_start():
            if self.lead != CELL_SEPARATOR:
                self.parts.append(self.lead)
        elif self.lead == CELL_SEPARATOR and self.parts[-1].endswith(":"):
            self.parts.append(" ")                # "Date:" · "Tuesday" reads as "Date: Tuesday"
        elif self.lead:
            self.parts.append(self.lead)
        elif self.space:
            self.parts.append(" ")
        self.lead = ""
        self.space = False
        self.parts.append(word)
        self.shown_chars += len(word)

    # --- events -------------------------------------------------------------

    def start(self, tag: str, attrs: dict[str, str | None]) -> None:
        tag = tag.lower()
        if tag not in _HEAD_TAGS:
            for index, (name, _) in enumerate(self.open):
                if name == "head":
                    self._close_to(index)
                    break
        if tag in _VOID_TAGS:
            if self.skipping:
                return
            if tag == "br":
                # Always a line of its own, even an empty one: that is how
                # Gmail and Outlook write a blank line (<div><br></div>).
                if self.parts:
                    self._break(0)
                    self.parts.append("\n")
            elif tag == "hr":
                self._break(2)
            return
        implied = _IMPLIED_END.get(tag)
        if implied:
            closes, bounds = implied
            for index in range(len(self.open) - 1, -1, -1):
                name = self.open[index][0]
                if name in bounds:
                    break
                if name in closes:
                    self._close_to(index)
                    break
        skips = (tag in _DROP_TAGS and tag not in self.transparent) or (
            self.honour_hidden and _is_hidden(attrs, self.classes))
        self.open.append((tag, skips))
        if skips:
            self.skipping += 1
        if self.skipping:
            return
        if tag in _PARAGRAPH_TAGS and not self._nested_list(tag):
            self._break(2)
        elif tag in _LINE_TAGS or tag in _PARAGRAPH_TAGS:
            self._break(1)
        if tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            depth = max(len(self.lists), 1)
            kind = self.lists[-1] if self.lists else ["ul", 0]
            if kind[0] == "ol":
                kind[1] = int(kind[1]) + 1
                mark = f"{kind[1]}."
            else:
                mark = "•"
            self.lead = "  " * (depth - 1) + mark + " "
        elif tag in _CELL_TAGS and not self._at_line_start():
            self.lead = CELL_SEPARATOR
        elif tag == "pre":
            self.pre += 1

    def _nested_list(self, tag: str) -> bool:
        """A list inside a list item sits on the next line, not after a gap."""
        return tag in ("ul", "ol") and any(name == "li" for name, _ in self.open)

    def end(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index][0] == tag:
                self._close_to(index)
                return
        # A stray end tag: nothing of that name is open. Ignore it.

    def _close_to(self, index: int) -> None:
        while len(self.open) > index:
            tag, skips = self.open.pop()
            if self.skipping:
                if skips:
                    self.skipping -= 1
                continue
            if tag in ("ul", "ol") and self.lists:
                self.lists.pop()
            if tag == "pre" and self.pre:
                self.pre -= 1
            if tag in _PARAGRAPH_TAGS and not self._nested_list(tag):
                self._break(2)
            elif tag in _LINE_TAGS or tag in _PARAGRAPH_TAGS:
                self._break(1)
            elif tag in _CELL_TAGS and self.lead == CELL_SEPARATOR:
                self.lead = ""

    def text(self, data: str) -> None:
        data = data.translate(_INVISIBLE)
        if self.skipping:
            if not (self.open and self.open[-1][0] in _DROP_TAGS):
                self.hidden_chars += len(data.strip())
            outer = next(name for name, skips in self.open if skips)
            # A drawing's own stylesheet is not the message it might have swallowed.
            if outer in _EMBEDDED_TAGS and self.open[-1][0] not in ("style", "script", "title"):
                self.embedded[outer] = self.embedded.get(outer, 0) + len(data.strip())
            return
        if self.pre:
            for number, line in enumerate(data.split("\n")):
                if number:
                    self._break(1)
                if line.strip():
                    self._word(line.rstrip())
            return
        if data[:1].isspace():
            self.space = True
        for number, word in enumerate(data.split()):
            if number:
                self.space = True
            self._word(word)
        if data[-1:].isspace():
            self.space = True

    @property
    def swallowed(self) -> frozenset[str]:
        """Embedded elements that held more text than the rest of the mail showed.

        An <svg> or <object> that is never closed takes everything after it
        along; a drawing with a caption does not hold the message.
        """
        return frozenset(tag for tag, chars in self.embedded.items() if chars > self.shown_chars)

    @property
    def result(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


class _Stdlib(HTMLParser):
    def __init__(self, writer: _Writer) -> None:
        super().__init__(convert_charrefs=True)
        self.writer = writer

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.writer.start(tag, dict(attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.writer.start(tag, dict(attrs))
        if tag.lower() not in _VOID_TAGS:
            self.writer.end(tag)

    def handle_endtag(self, tag: str) -> None:
        self.writer.end(tag)

    def handle_data(self, data: str) -> None:
        self.writer.text(data)


_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)


_RAW_TEXT_OPEN = re.compile(r"<(style|script)\b[^>]*>", re.I)
_HEAD_ENDS = re.compile(r"</head\b|<body\b", re.I)


def _close_raw_text(html: str) -> str:
    """End a <style> or <script> its author never ended.

    The stdlib parser reads everything after one as stylesheet, to the end of
    the mail. A browser does too, strictly, but mail clients do not, and the
    message is there to be read: the element ends where the head does.
    """
    out, at = [], 0
    for match in _RAW_TEXT_OPEN.finditer(html):
        if match.start() < at:
            continue
        closing = re.compile(rf"</{match.group(1)}\s*>", re.I).search(html, match.end())
        if closing:
            out.append(html[at:closing.end()])
            at = closing.end()
            continue
        stop = _HEAD_ENDS.search(html, match.end())
        end = stop.start() if stop else match.end()
        out.append(html[at:end] + f"</{match.group(1)}>")
        at = end
    return "".join(out) + html[at:]


def _stdlib_text(html: str, honour_hidden: bool,
                 transparent: frozenset[str] = frozenset()) -> tuple[str, _Writer]:
    writer = _Writer(hidden_classes(_STYLE_BLOCK.findall(html)), honour_hidden, transparent)
    parser = _Stdlib(writer)
    parser.feed(html)
    parser.close()
    return writer.result, writer


def _selectolax_text(html: str, honour_hidden: bool,
                     transparent: frozenset[str] = frozenset()) -> tuple[str, _Writer]:
    tree = _Selectolax(html)
    writer = _Writer(hidden_classes([node.text() for node in tree.css("style")]), honour_hidden,
                     transparent)
    root = tree.root
    if root is None:
        return "", writer
    # Iterative, not recursive: mail nests layout tables deeply enough to
    # matter, and the text of one message must never cost a RecursionError.
    stack: list[tuple[object, bool]] = [(root, False)]
    while stack:
        node, leaving = stack.pop()
        tag = node.tag  # type: ignore[attr-defined]
        if leaving:
            writer.end(tag)
            continue
        if tag == "-text":
            writer.text(node.text(deep=False) or "")  # type: ignore[attr-defined]
            continue
        if not tag or tag.startswith(("_", "!")):
            continue
        writer.start(tag, dict(node.attributes))  # type: ignore[attr-defined]
        if tag in _VOID_TAGS:
            continue
        stack.append((node, True))
        children = list(node.iter(include_text=True))  # type: ignore[attr-defined]
        stack.extend((child, False) for child in reversed(children))
    return writer.result, writer


def html_to_text(html: str) -> str:
    """Return readable plain text for an HTML mail part."""
    if not html:
        return ""
    convert = _selectolax_text if _Selectolax is not None else _stdlib_text
    try:
        html = _close_raw_text(html)
        text, writer = convert(html, True)
        # Hiding is a judgement about styles. If it hid more than it showed,
        # that judgement was wrong for this mail: show everything instead.
        if writer.hidden_chars > max(writer.shown_chars, 200):
            text, writer = convert(html, False)
        if writer.swallowed:
            text, _ = convert(html, writer.honour_hidden, writer.swallowed)
        return text
    except Exception:
        stripped = re.sub(r"<[^>]+>", " ", html).translate(_INVISIBLE)
        return re.sub(r"\s+", " ", unescape(stripped)).strip()


_LOOKS_LIKE_HTML = re.compile(
    r"^\s*(?:<!doctype\s+html|<html\b|<head\b|<body\b|<meta\b|<table\b|<div\b|<p\b)", re.I)


def looks_like_html(text: str) -> bool:
    """True for a "text/plain" part that is really an HTML document."""
    return bool(_LOOKS_LIKE_HTML.match(text or "")) and "</" in text
