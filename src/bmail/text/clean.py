"""Quote stripping, signature removal, subject normalisation, language guess.

Quoted history is the single biggest source of noise in mail summarisation: a
three-reply thread is typically 80% repetition. Everything here is heuristic,
bilingual (English/German) and conservative — when in doubt it keeps the text.
"""

from __future__ import annotations

import re
import unicodedata

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
    r"|web ?version|online[- ]version|webansicht)"
    r"[^\w\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Characters with no width that mail uses to pad preview text. U+200D is kept:
# emoji sequences are built with it.
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u034f\u180e\u200b\u200c\u2060\u2061\u2062\u2063\ufeff"))

_URL = re.compile(r"https?://\S+")
_TRACKING_PIXEL = re.compile(r"\[image:[^\]]*\]", re.IGNORECASE)


def normalise_whitespace(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_INVISIBLE)
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
    text = _TRACKING_PIXEL.sub(" ", text)
    text = _WEB_VIEW.sub("", text)
    text = strip_quoted(text)
    text = strip_signature(text)
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
