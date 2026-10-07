"""A message as its sender laid it out, made safe to show.

The text Bmail reads is what a message says. This is what it looks like: the
HTML part, kept so the reader can show a job alert as a list of jobs with logos
and buttons instead of forty lines of text.

Showing someone else's HTML inside the app is the most dangerous thing the app
does, so three separate things each have to fail before anything goes wrong:

* the page is served with a `sandbox` policy: no scripts, no forms, no windows;
* its content policy allows inline styles and pictures and nothing else, so a
  script would be refused even outside the sandbox. Remote pictures can be
  switched off: fetching one tells the sender that the mail was opened, when,
  and from which address;
* the markup is rebuilt anyway: read tag by tag and written out again without
  scripts, frames, redirects, event handlers or anything that says where a
  request came from.

The rebuilding reads each character once. A mail is hostile input of up to two
megabytes, and a pattern that looks ahead for a ">" from every "<" takes hours
on one written to make it.
"""

from __future__ import annotations

import base64
import re
import zlib
from html import escape

# Larger than this is not a message anyone laid out by hand; the text is enough.
MAX_HTML_BYTES = 2_000_000
MAX_INLINE_IMAGE_BYTES = 300_000
MAX_INLINE_TOTAL_BYTES = 1_500_000

# Never written out, with or without what is inside them.
_DROPPED_WITH_CONTENT = ("script",)
# Never written out; what they contain is ordinary content and stays.
_DROPPED = frozenset({
    "iframe", "frame", "frameset", "object", "embed", "applet", "base", "link", "form",
    "meta", "svg", "math", "portal", "template", "noscript",
})
_DROPPED_ATTRIBUTES = frozenset({
    "referrerpolicy", "ping", "srcdoc", "formaction", "action", "manifest", "http-equiv",
    "integrity", "crossorigin", "nonce", "autofocus",
})
_ADDRESS_ATTRIBUTES = frozenset({
    "href", "src", "background", "poster", "data", "cite", "longdesc", "lowsrc",
})
# What a remote picture is named by; set aside when pictures are not wanted.
_PICTURE_ATTRIBUTES = frozenset({"src", "srcset", "background", "poster", "lowsrc"})

_NAME = re.compile(r"/?\s*([a-zA-Z][a-zA-Z0-9:_-]{0,40})")
# One attribute. Every alternative consumes what it matches, and none looks
# past the end of its own token, so a tag of any length is read in one pass.
_ATTRIBUTE = re.compile(r"""([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")
_NOT_AN_ADDRESS = re.compile(r"[\x00-\x20\x7f]+")
_REMOTE = re.compile(r"(?:https?:)?//", re.IGNORECASE)
_REMOTE_IN_STYLE = re.compile(r"url\(\s*[\"']?\s*(?:https?:)?//", re.IGNORECASE)
_END_OF_SCRIPT = re.compile(r"</script\s*>", re.IGNORECASE)
_CID = re.compile(r"""cid:([^"'\s>)]+)""", re.IGNORECASE)

# What every message is shown on: paper, whatever the app's own colours are.
# Mail is written for a white page and says so nowhere.
_FRAME = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="referrer" content="no-referrer"><base target="_blank">'
    # Only defaults, the ones a webmail page gives a message: anything the
    # message says about itself wins. No rule here may touch its layout; a
    # "max-width: 100%" on pictures shrinks every logo that sits in a table.
    "<style>html{background:#fff;color:#222;font:14px/1.5 Arial,Helvetica,sans-serif}"
    "body{margin:0;padding:12px;overflow-wrap:break-word}</style></head><body>"
)


def pack(html: str) -> bytes | None:
    """The HTML as it is stored, or None when it is too large to keep."""
    data = html.encode("utf-8", errors="replace")
    if not html.strip() or len(data) > MAX_HTML_BYTES:
        return None
    return zlib.compress(data, 6)


def unpack(stored: bytes) -> str:
    return zlib.decompress(stored).decode("utf-8", errors="replace")


def inline_images(html: str, images: dict[str, tuple[str, bytes]]) -> str:
    """Put the pictures a message carries with it into its HTML.

    A logo or a signature attached to the message is referred to as
    `cid:<id>`. Attachments are not stored, so without this those pictures
    would be gone the moment the message was read in. Only small images: a
    message that carries a camera photograph is not asking to have it inlined.
    """
    if not images or "cid:" not in html.lower():
        return html
    budget = MAX_INLINE_TOTAL_BYTES

    def embed(match: re.Match[str]) -> str:
        nonlocal budget
        found = images.get(match.group(1).strip().lower())
        if found is None:
            return match.group(0)
        kind, payload = found
        if len(payload) > MAX_INLINE_IMAGE_BYTES or len(payload) > budget:
            return match.group(0)
        budget -= len(payload)
        return f"data:{kind};base64,{base64.b64encode(payload).decode('ascii')}"

    return _CID.sub(embed, html)


def _end_of_tag(html: str, start: int) -> int:
    """Index of the ">" that closes the tag opened at `start`, or -1.

    A ">" inside a quoted attribute value is part of the value. A quote only
    opens a value straight after "=", so a stray one cannot swallow the rest
    of the message.
    """
    quote = ""
    after_equals = False
    for index in range(start + 1, len(html)):
        ch = html[index]
        if quote:
            if ch == quote:
                quote = ""
        elif ch == ">":
            return index
        elif ch in ('"', "'") and after_equals:
            quote = ch
        if not ch.isspace():
            after_equals = ch == "="
    return -1


def _quoted(value: str) -> str:
    """An attribute value as it goes between double quotes. Its "&" stays as it
    was written: it already begins an entity, or stands for itself."""
    return value.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _attributes(inside: str, *, pictures: bool) -> tuple[list[str], bool]:
    """The attributes worth keeping, written out again, and whether any
    of them names a remote picture."""
    kept: list[str] = []
    remote = False
    for match in _ATTRIBUTE.finditer(inside):
        name = match.group(1).lower()
        value = next((group for group in match.groups()[1:] if group is not None), None)
        if name.startswith("on") or ":" in name or name in _DROPPED_ATTRIBUTES:
            continue
        if value is None:
            kept.append(name)
            continue
        if name in _ADDRESS_ATTRIBUTES:
            # Browsers ignore spaces and control characters inside a scheme.
            bare = _NOT_AN_ADDRESS.sub("", value).lower()
            if bare.startswith(("javascript:", "vbscript:", "data:text", "data:application")):
                value = "#"
        if name == "style" and _REMOTE_IN_STYLE.search(value):
            remote = True
        if name in _PICTURE_ATTRIBUTES and (name == "srcset" or _REMOTE.match(value.lstrip())):
            remote = True
            if not pictures:
                # The policy already refuses them; without the address the browser
                # draws the picture's description instead of a broken-image mark.
                name = f"data-not-loaded-{name}"
        kept.append(f'{name}="{_quoted(value)}"')
    return kept, remote


def _rebuild(html: str, *, pictures: bool) -> tuple[str, bool]:
    """`html` written out again without anything that could act.

    One pass: text is copied as it stands, every tag is taken apart and put
    back together from its name and the attributes that are allowed, so what
    comes out contains only tags written here.
    """
    out: list[str] = []
    remote = False
    at, size = 0, len(html)
    while at < size:
        lt = html.find("<", at)
        if lt < 0:
            out.append(html[at:])
            break
        out.append(html[at:lt])
        following = html[lt + 1 : lt + 2]
        if html.startswith("<!--", lt):
            end = html.find("-->", lt + 4)
            if end < 0:
                break                      # a comment to the end of the message
            at = end + 3
            continue
        if following in ("!", "?"):
            end = html.find(">", lt + 1)   # a doctype or a processing instruction
            if end < 0:
                break
            at = end + 1
            continue
        if not (following.isalpha() or following == "/"):
            out.append("&lt;")             # "a < b": a character, not a tag
            at = lt + 1
            continue
        end = _end_of_tag(html, lt)
        if end < 0:
            out.append(escape(html[lt:]))  # never closed: the rest is text
            break
        inside = html[lt + 1 : end]
        at = end + 1
        named = _NAME.match(inside)
        if named is None:
            continue
        name = named.group(1).lower()
        closing = inside.lstrip().startswith("/")
        if name in _DROPPED_WITH_CONTENT:
            if not closing:
                finish = _END_OF_SCRIPT.search(html, at)
                if finish is None:
                    break                  # a script to the end of the message
                at = finish.end()
            continue
        if name in _DROPPED or ":" in name:
            continue
        if closing:
            out.append(f"</{name}>")
            continue
        kept, names_remote = _attributes(inside[named.end():], pictures=pictures)
        remote = remote or names_remote
        out.append(f"<{name}{' ' if kept else ''}{' '.join(kept)}>")
    rebuilt = "".join(out)
    return rebuilt, remote or _REMOTE_IN_STYLE.search(rebuilt) is not None


def has_remote_pictures(html: str) -> bool:
    return _rebuild(html, pictures=True)[1]


def clean(html: str, *, pictures: bool = True) -> str:
    """Cut out everything that could act; what is left can only be looked at."""
    return _rebuild(html, pictures=pictures)[0]


def page(html: str, *, pictures: bool) -> bytes:
    """The document the reader's frame shows."""
    return (_FRAME + clean(html, pictures=pictures) + "</body></html>").encode("utf-8")


def policy(*, pictures: bool) -> str:
    """The Content-Security-Policy the page is served with."""
    # Never plain http: that is what a router, a printer and Bmail's own engine
    # answer on, and a picture address is a request made from this machine.
    # A picture addressed with http is asked for over https instead: every
    # mail service answers there, and a device on the home network does not.
    images = "img-src data: https:; upgrade-insecure-requests" if pictures else "img-src data:"
    # `allow-same-origin` and nothing else. It lets the reader, which is the
    # page around the frame, measure the message to give it its full height
    # and hear a click on a link. The message itself still cannot run a line
    # of script, submit a form or open a window, so it has no way to use an
    # origin: this is how webmail shows mail.
    return (
        "sandbox allow-same-origin; default-src 'none'; "
        f"style-src 'unsafe-inline'; {images}; font-src data:; form-action 'none'; "
        "base-uri 'none'; frame-ancestors 'self'"
    )
