"""Quote stripping, signature removal, subject normalisation, language guess.

Quoted history is the single biggest source of noise in mail summarisation: a
three-reply thread is typically 80% repetition. Everything here is heuristic,
bilingual (English/German) and conservative — when in doubt it keeps the text.
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape

from bmail.text.html import html_to_text, looks_like_html

# Raise this whenever `clean_body` changes what it produces: the store cleans
# every body it holds again, once, the next time it is opened.
TEXT_VERSION = 3

# --- quoted-reply attribution lines -------------------------------------- #
# Matching the *attribution* line and cutting there beats matching ">" prefixes,
# because HTML mail frequently loses the ">" markers entirely.
_ATTRIBUTION = re.compile(
    r"""^\s*(
        (On\ .{4,80}\ wrote:)
      | (Am\ .{4,80}\ schrieb\ .{0,80}:)
      | (-{2,}\s*(Original\ Message|Urspr(ü|ue)ngliche\ Nachricht|Forwarded\ message|Weitergeleitete\ Nachricht)\s*-{2,})
      | ((From|Von|De)\ *:\ *.{3,120}\n\s*(Sent|Gesendet|Date|Datum)\ *:)
      | (_{10,})
    )""",
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)

_SIGNATURE_DELIM = re.compile(r"^-- ?$", re.MULTILINE)

_SIGNATURE_HINT = re.compile(
    r"^\s*(mit\s+freundlichen\s+gr(ü|ue)(ß|ss)en|viele\s+gr(ü|ue)(ß|ss)e|beste\s+gr(ü|ue)(ß|ss)e|"
    r"best\s+regards|kind\s+regards|warm\s+regards|sincerely|yours\s+(sincerely|faithfully)|"
    r"freundliche\s+gr(ü|ue)(ß|ss)e)\b",
    re.IGNORECASE | re.MULTILINE,
)

_DISCLAIMER = re.compile(
    r"^\s*(this\s+e?-?mail\s+(and\s+any\s+attachments\s+)?(is|are)\s+confidential|"
    r"diese\s+e-?mail\s+(und\s+.{0,40})?enth(ä|ae)lt\s+vertrauliche|"
    r"if\s+you\s+are\s+not\s+the\s+intended\s+recipient|"
    r"sie\s+erhalten\s+diese\s+(e-?mail|nachricht),\s+weil|"
    r"unsubscribe|abmelden|newsletter\s+abbestellen)",
    re.IGNORECASE | re.MULTILINE,
)

_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(re|aw|antw|fwd?|wg|sv|vs|res|tr|rif)\s*(\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)

# Lines that only point at another rendering of the same mail. They carry no
# meaning, and they would otherwise be the "first line" a summary quotes.
_WEB_VIEW = re.compile(
    r"^[^\w\n]*(?:having trouble[^.?!\n]{0,40}[.?!]\s*|problems? viewing[^.?!\n]{0,40}[.?!]\s*)?"
    r"(?:click here to |klicken sie hier,? um (?:diese )?(?:e-?mail|nachricht) )?"
    r"(?:(?:view|read|open|see)(?: (?:this|the|it|our))?(?: (?:e-?mail|message|newsletter|mail))?"
    r"(?: online| (?:in|with) (?:your |a |the )?(?:web ?)?browser| as a web ?page)"
    r"|(?:(?:diese |die )?(?:e-?mail|nachricht|newsletter) )?(?:im|in ihrem) (?:web)?browser "
    r"(?:an(?:zeigen|sehen)|lesen|öffnen)(?: zu können)?"
    r"|online (?:an)?(?:sehen|zeigen|lesen|öffnen)"
    r"|web ?version|online[- ]version|webansicht)"
    r"(?:\s*:?\s*<?https?://\S+>?)?"
    r"[^\w\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Characters with no width that mail uses to pad preview text. U+200D is kept:
# emoji sequences are built with it.
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u034f\u180e\u200b\u200c\u200e\u200f\u2060\u2061\u2062\u2063\ufeff"))
# A joiner with nothing on one side joins nothing: preview padding again.
_STRAY_JOINER = re.compile(r"(?:(?<=\s)|^)\u200d+|\u200d+(?=\s|$)", re.MULTILINE)

_URL = re.compile(r"https?://\S+")
_TRACKING_PIXEL = re.compile(r"\[image:[^\]]*\]", re.IGNORECASE)

# A plain-text part generated from HTML often carries the stylesheet with it.
# A declaration ("font-size: 100% !important;"), a rule with its braces, a
# selector list waiting for its brace: none of it is anything a person wrote.
_CSS_DECLARATION = re.compile(r"[a-z-]{3,}\s*:\s*[^;{}\n]+(?:;|!important|\})", re.IGNORECASE)
_CSS_BRACE_LINE = re.compile(r"^[\w\s,.#:>*+~()\[\]=\"'%@-]*\{[^{}\n]*$|^[^{}\n]*\}\s*[\w\s,.#:>*+~-]*\{?\s*$")
_CSS_SELECTOR_LIST = re.compile(r"^[\w\s.#:>*+~\[\]=\"'-]+(?:,\s*[\w\s.#:>*+~\[\]=\"'-]+)*,\s*$")
_CSS_AT_RULE = re.compile(r"^\s*@(?:media|font-face|import|supports|keyframes|charset)\b", re.IGNORECASE)

# A long link wrapped at the plain-text line limit leaves a line that is one
# unbroken run of query-string characters.
_URL_TAIL = re.compile(r"^[\w=&%./~?#+:;@!$-]{16,}$")
# The part of a link a person can read; everything after it is tracking.
_READABLE_PATH = re.compile(r"^/?[\w./-]{0,40}$")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}")
_RULE_LINE = re.compile(r"^[-=*~#.•·]{10,}$", re.MULTILINE)


# Markup in what was sent as plain text. Marketing systems build the plain part
# from the HTML one and leave tags and entities in it; a few send the whole
# HTML document labelled as text. Only names HTML has: "List<String>" and
# "<https://a.link>" are text.
_TAG_NAMES = (
    r"(?:a|abbr|b|blockquote|body|br|center|code|dd|details|div|dl|dt|em|font|h[1-6]|head|hr|"
    r"html|i|img|li|link|meta|ol|p|pre|small|span|strong|style|sub|summary|sup|table|tbody|td|"
    r"tfoot|th|thead|title|tr|u|ul)"
)
_HTML_TAG = re.compile(rf"</?{_TAG_NAMES}(?:\s[^<>]{{0,2000}})?/?>", re.IGNORECASE)
_NOT_A_TAG = re.compile(rf"<(?!/?{_TAG_NAMES}(?:\s[^<>]*)?/?>|!--)", re.IGNORECASE)
# "&" that begins no complete entity. An HTML reader also accepts the old
# entities without their semicolon, and so reads "?id=5&region=de" as "®ion".
_BARE_AMPERSAND = re.compile(r"&(?!(?:[a-z]{2,8}|#\d{2,6}|#x[0-9a-f]{2,5});)", re.IGNORECASE)
_ENTITY = re.compile(r"&(?:[a-z]{2,8}|#\d{2,6}|#x[0-9a-f]{2,5});", re.IGNORECASE)

# What a mail-merge left unfilled, on a line of its own: %opentrack%,
# {{first_name}}, *|UNSUB|*.
_PLACEHOLDER_LINE = re.compile(
    r"^[ \t]*(?:%[a-z_]{3,30}%|\{\{[^{}\n]{1,40}\}\}|\*\|[\w:]{2,30}\|\*)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# A line that is one link and nothing else.
_LINK_LINE = re.compile(r"^\(?\s*(?:https?://)?[\w.-]+\.[a-z]{2,}(?:/\S*)?\s*\)?$", re.IGNORECASE)


def has_markup(text: str) -> bool:
    """Whether text sent as plain text carries HTML tags."""
    found = 0
    for _ in _HTML_TAG.finditer(text or ""):
        found += 1
        if found == 2:
            return True
    return False


def strip_markup(text: str) -> str:
    """Read HTML that arrived as plain text the way it was meant to be seen."""
    if looks_like_html(text):
        return html_to_text(text)
    if has_markup(text):
        # The line breaks are the sender's layout here, and an HTML reader
        # ignores them; "<" that opens no tag is a character, not markup, and
        # so is the "&" in a link.
        text = _NOT_A_TAG.sub("&lt;", _BARE_AMPERSAND.sub("&amp;", text))
        return html_to_text(text.replace("\n", "<br>\n"))
    if "&" in text and _ENTITY.search(text):
        return _ENTITY.sub(lambda match: unescape(match.group(0)), text)
    return text


def drop_repeated_link_lines(text: str) -> str:
    """Remove a bare link that stands alone three times or more.

    A plain part made from an image-heavy mail has one for every picture, all
    the same tracking address: a column of "click.news.shop.com" between the
    sentences. A link given once is something to follow and stays.
    """
    lines = text.split("\n")
    counts: dict[str, int] = {}
    for line in lines:
        bare = line.strip()
        if bare and _LINK_LINE.match(bare):
            counts[bare] = counts.get(bare, 0) + 1
    repeated = {bare for bare, count in counts.items() if count >= 3}
    if not repeated:
        return text
    return "\n".join(line for line in lines if line.strip() not in repeated)


def _is_css_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _CSS_AT_RULE.match(stripped):
        return True
    if "{" in stripped or "}" in stripped:
        return bool(_CSS_DECLARATION.search(stripped) or _CSS_BRACE_LINE.match(stripped))
    # A declaration on its own line, between the braces of a rule.
    return bool(re.fullmatch(r"(?:[a-z-]{3,}\s*:\s*[^;{}]+;\s*)+", stripped, re.IGNORECASE))


def strip_css(text: str) -> str:
    """Remove stylesheet lines a sender's software left in the plain text."""
    if "{" not in text and "!important" not in text:
        return text
    lines = text.split("\n")
    css = [_is_css_line(line) for line in lines]
    # A selector list that runs onto the next line ("body, table, td,") is CSS
    # only when the rule it belongs to follows it.
    for i in range(len(lines) - 2, -1, -1):
        if not css[i] and css[i + 1] and _CSS_SELECTOR_LIST.match(lines[i].strip()):
            css[i] = True
    if not any(css):
        return text
    return "\n".join(line for line, drop in zip(lines, css, strict=True) if not drop)


def join_wrapped_links(text: str) -> str:
    """Put back together a link that the plain-text line limit broke in two."""
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        tail = line.strip()
        if out and out[-1].strip() and out[-1].split()[-1].startswith("http") \
                and _URL_TAIL.match(tail) and not tail.startswith("http"):
            out[-1] = out[-1].rstrip() + line.strip()
            continue
        out.append(line)
    return "\n".join(out)


def shorten_links(text: str) -> str:
    """Show a link as where it goes, not as three lines of tracking code.

    The address a person can read is kept ("netflix.com/title/81977145"); the
    query string and any path that is only an opaque token is not. The full
    text, with every link intact, is kept beside this one.
    """

    def _short(match: re.Match[str]) -> str:
        url = match.group(0)
        trail = ""
        while url and url[-1] in ").,;:!?]>'\"":
            trail = url[-1] + trail
            url = url[:-1]
        if len(url) <= 60:
            return url + trail
        rest = re.sub(r"^https?://(www\.)?", "", url)
        # The host ends at the path, or at the query when there is no path:
        # "shop.com?utm_campaign=..." kept all of its tracking as "the host".
        host = re.split(r"[/?#]", rest, maxsplit=1)[0]
        path = rest[len(host):].split("?")[0].split("#")[0]
        if not path.strip("/"):
            return host + trail
        if _READABLE_PATH.match(path) and not _TOKEN.search(path):
            return f"{host}{path.rstrip('/')}" + trail
        return f"{host}/..." + trail

    return _URL.sub(_short, text)


def normalise_whitespace(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_INVISIBLE)
    if "‍" in text:
        text = _STRAY_JOINER.sub("", text)
    text = text.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_quoted(text: str) -> str:
    """Cut the message at the first quoted-reply attribution line."""
    if not text:
        return ""
    match = _ATTRIBUTION.search(text)
    head = text[: match.start()] if match else text
    # Drop any surviving ">"-prefixed block at the tail.
    lines = head.split("\n")
    while lines and lines[-1].lstrip().startswith(">"):
        lines.pop()
    kept = [ln for ln in lines if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def strip_signature(text: str) -> str:
    """Remove a trailing signature block, but never the whole message."""
    if not text:
        return ""
    candidates: list[int] = []
    delim = _SIGNATURE_DELIM.search(text)
    if delim:
        candidates.append(delim.start())
    for pattern in (_SIGNATURE_HINT, _DISCLAIMER):
        for hit in pattern.finditer(text):
            candidates.append(hit.start())
    if not candidates:
        return text.strip()
    cut = min(candidates)
    # A signature that starts in the first 40% of the body is probably not a
    # signature (short notes often open with a greeting phrase); keep the text.
    if cut < max(80, int(len(text) * 0.4)):
        return text.strip()
    return text[:cut].strip()


def clean_body(text: str) -> str:
    """Full cleaning pass used by the normaliser."""
    text = normalise_whitespace(text)
    text = normalise_whitespace(strip_markup(text))
    text = _TRACKING_PIXEL.sub(" ", text)
    text = _PLACEHOLDER_LINE.sub("", text)
    text = strip_css(text)
    text = join_wrapped_links(text)
    text = _WEB_VIEW.sub("", text)
    text = strip_quoted(text)
    text = strip_signature(text)
    text = shorten_links(text)
    text = drop_repeated_link_lines(text)
    text = _RULE_LINE.sub("", text)
    return normalise_whitespace(text)


def normalise_subject(subject: str) -> str:
    """`Re: AW: Fwd: Ihre Bewerbung` -> `ihre bewerbung`, for dedupe and keys."""
    subject = normalise_whitespace(subject).lower()
    previous = None
    while previous != subject:
        previous = subject
        subject = _SUBJECT_PREFIX.sub("", subject).strip()
    subject = re.sub(r"\[[^\]]{0,40}\]", " ", subject)  # [EXTERNAL], [Ticket #123]
    return re.sub(r"\s+", " ", subject).strip()


def redact_urls(text: str) -> str:
    """Replace URLs with their host, so no full link ever reaches a prompt."""

    def _host(match: re.Match[str]) -> str:
        url = match.group(0)
        host = re.sub(r"^https?://", "", url).split("/")[0]
        return f"[link:{host}]"

    return _URL.sub(_host, text)


# --- language --------------------------------------------------------------- #

_DE_MARKERS = (
    " der ", " die ", " das ", " und ", " nicht ", " wir ", " sie ", " ihre ", " ihnen ",
    " für ", " mit ", " bei ", " bewerbung", " stelle", " sehr geehrte", " freundlichen",
    " leider ", " zusage", " absage", " unterlagen", " bitte ",
)
_EN_MARKERS = (
    " the ", " and ", " you ", " your ", " we ", " for ", " with ", " application",
    " position", " dear ", " regards", " thank ", " please ", " unfortunately ",
    " interview", " role ",
)


def detect_language(text: str) -> str:
    """Cheap German/English discriminator. Returns 'de', 'en' or 'und'.

    A real langid model is overkill here: the only downstream decision is which
    prompt variant and which recipe set to prefer, and both tolerate 'und'.
    """
    sample = f" {normalise_whitespace(text).lower()[:2000]} "
    if len(sample) < 30:
        return "und"
    de = sum(sample.count(marker) for marker in _DE_MARKERS)
    en = sum(sample.count(marker) for marker in _EN_MARKERS)
    umlauts = sum(sample.count(ch) for ch in "äöüß")
    de += umlauts
    if de == en == 0:
        return "und"
    if de >= en * 1.3 and de >= 2:
        return "de"
    if en >= de * 1.3 and en >= 2:
        return "en"
    return "und"
