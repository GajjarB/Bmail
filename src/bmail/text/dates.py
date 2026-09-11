"""Bilingual (EN/DE) date and time extraction.

Dates are where extraction quality is actually decided: a summary that is
slightly off is annoying, an interview time that is off is a missed interview.
So dates are parsed deterministically here whenever the text allows it, and the
language model is only asked for the cases this cannot reach.

Every function returns the matched source text alongside the value, so the UI
can always show why it believes a date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

MONTHS: dict[str, int] = {
    # English
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    # German
    "jan.": 1, "januar": 1, "jänner": 1, "februar": 2, "feb.": 2, "märz": 3,
    "maerz": 3, "mrz": 3, "mai": 5, "juni": 6, "juli": 7, "okt": 10,
    "oktober": 10, "dez": 12, "dezember": 12,
}

_MONTH_ALT = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))

_DMY = re.compile(r"\b(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{2,4})\b")
_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_DMY_NAME = re.compile(rf"\b(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})\b", re.IGNORECASE)
_DMY_NAME_NOYEAR = re.compile(rf"\b(\d{{1,2}})\.?\s+({_MONTH_ALT})\b\.?(?!\s+\d{{4}})", re.IGNORECASE)
_MDY_NAME = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE)
_MD_NAME_NOYEAR = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!,?\s+\d{{4}})", re.IGNORECASE)

_TIME = re.compile(
    r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|a\.m\.|p\.m\.|uhr)?|\b(\d{1,2})\s*(am|pm|uhr)\b",
    re.IGNORECASE,
)

# Timezone abbreviations that actually appear in interview invitations. An
# interview time is the one field where being an hour out is a real failure, so
# a stated zone is honoured rather than assumed to be UTC.
#
# Abbreviations are ambiguous in general (IST and CST mean several things); only
# the unambiguous European and North American ones are mapped, and anything else
# falls back to the configured local zone.
TZ_OFFSETS: dict[str, int] = {
    "utc": 0, "gmt": 0, "z": 0,
    "bst": 60, "wet": 0, "west": 60,
    "cet": 60, "mez": 60, "cest": 120, "mesz": 120,
    "eet": 120, "eest": 180,
    "est": -300, "edt": -240, "cdt": -300, "mst": -420, "mdt": -360,
    "pst": -480, "pdt": -420,
}

# The explicit-offset branch must come first: in "UTC+2" the bare "UTC" would
# otherwise match and the "+2" would be silently dropped.
_TZ_TOKEN = re.compile(
    r"(?:UTC|GMT)\s*([+-]\d{1,2})(?::?(\d{2}))?"
    r"|\b(" + "|".join(sorted(TZ_OFFSETS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_RELATIVE_DAYS = {
    "today": 0, "heute": 0, "tomorrow": 1, "morgen": 1, "übermorgen": 2, "uebermorgen": 2,
}

_WITHIN = re.compile(
    r"(?:within|innerhalb\s+von|in\s+den\s+n(?:ä|ae)chsten|in\s+the\s+next)\s+(\d{1,3})\s*"
    r"(day|days|working\s+days|business\s+days|tag|tage|tagen|werktage|werktagen|week|weeks|woche|wochen)",
    re.IGNORECASE,
)

_DEADLINE_CUE = re.compile(
    r"(deadline|due\s+(?:by|on|date)|no\s+later\s+than|by\s+the\s+latest|closing\s+date|"
    r"expires?\s+on|frist|bis\s+sp(?:ä|ae)testens|einsendeschluss|bewerbungsschluss|"
    r"f(?:ä|ae)llig\s+am|zahlbar\s+bis|abgabefrist|bis\s+zum|within\s+\d|"
    r"innerhalb\s+von\s+\d|not\s+later\s+than|submit\s+by|reply\s+by)",
    re.IGNORECASE,
)

_EVENT_CUE = re.compile(
    r"(interview|call|meeting|gespr(?:ä|ae)ch|termin|vorstellungsgespr(?:ä|ae)ch|"
    r"appointment|session|slot|invite[ds]?\s+you)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DateHit:
    """A parsed date and the exact text it came from."""

    value: date
    span: tuple[int, int]
    text: str
    has_time: bool = False
    at: time | None = None
    tzoffset_minutes: int | None = None   # from a zone named in the text, if any

    @property
    def tzinfo(self) -> timezone | None:
        if self.tzoffset_minutes is None:
            return None
        return timezone(timedelta(minutes=self.tzoffset_minutes))

    def as_datetime(self, default_tz: timezone = timezone.utc) -> datetime:
        """Combine date and time, in the zone the email named where it named one."""
        zone = self.tzinfo or default_tz
        return datetime.combine(self.value, self.at or time(0, 0), tzinfo=zone)

    def as_utc(self, default_tz: timezone = timezone.utc) -> datetime:
        return self.as_datetime(default_tz).astimezone(timezone.utc)


def _year(raw: str) -> int:
    year = int(raw)
    if year < 100:
        year += 2000 if year < 70 else 1900
    return year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str, *, reference: date | None = None) -> list[DateHit]:
    """All dates in `text`, in source order, de-duplicated by span.

    `reference` (default: today) resolves year-less dates such as "14 September"
    forward, which is almost always what mail means.
    """
    if not text:
        return []
    reference = reference or datetime.now(timezone.utc).date()
    hits: list[DateHit] = []
    claimed: list[tuple[int, int]] = []

    def add(value: date | None, match: re.Match[str]) -> None:
        if value is None:
            return
        span = match.span()
        if any(span[0] < end and start < span[1] for start, end in claimed):
            return
        claimed.append(span)
        hits.append(DateHit(value=value, span=span, text=match.group(0)))

    for match in _ISO.finditer(text):
        add(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))), match)
    for match in _DMY.finditer(text):
        first, second, third = int(match.group(1)), int(match.group(2)), _year(match.group(3))
        # Day-first is the European convention and dominates this corpus; fall
        # back to month-first only when day-first is impossible.
        value = _safe_date(third, second, first) or _safe_date(third, first, second)
        add(value, match)
    for match in _DMY_NAME.finditer(text):
        month = MONTHS.get(match.group(2).lower().rstrip("."))
        if month:
            add(_safe_date(_year(match.group(3)), month, int(match.group(1))), match)
    for match in _MDY_NAME.finditer(text):
        month = MONTHS.get(match.group(1).lower().rstrip("."))
        if month:
            add(_safe_date(_year(match.group(3)), month, int(match.group(2))), match)
    for match in _DMY_NAME_NOYEAR.finditer(text):
        month = MONTHS.get(match.group(2).lower().rstrip("."))
        if month:
            add(_forward(reference, month, int(match.group(1))), match)
    for match in _MD_NAME_NOYEAR.finditer(text):
        month = MONTHS.get(match.group(1).lower().rstrip("."))
        if month:
            add(_forward(reference, month, int(match.group(2))), match)

    for word, offset in _RELATIVE_DAYS.items():
        for match in re.finditer(rf"\b{word}\b", text, re.IGNORECASE):
            add(reference + timedelta(days=offset), match)
    for match in _WITHIN.finditer(text):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        days = amount * 7 if unit.startswith(("week", "woche")) else amount
        add(reference + timedelta(days=days), match)

    hits.sort(key=lambda hit: hit.span[0])
    _attach_times(text, hits, claimed)
    return hits


def _forward(reference: date, month: int, day: int) -> date | None:
    """Resolve a year-less date to the next occurrence, allowing a short past window."""
    for year in (reference.year, reference.year + 1):
        value = _safe_date(year, month, day)
        if value and value >= reference - timedelta(days=14):
            return value
    return _safe_date(reference.year, month, day)


def _attach_times(
    text: str, hits: list[DateHit], date_spans: list[tuple[int, int]] | None = None
) -> None:
    """Bind a clock time to the date it sits closest to (within ~60 characters).

    Time matches overlapping a date span are discarded: in `15.10.2026` the
    `15.10` is not a quarter past ten.
    """
    date_spans = date_spans or []
    for match in _TIME.finditer(text):
        start, end = match.span()
        if any(start < span_end and span_start < end for span_start, span_end in date_spans):
            continue
        parsed = _parse_time(match)
        if parsed is None:
            continue
        position = match.start()
        best: DateHit | None = None
        best_distance = 61
        for hit in hits:
            distance = min(abs(position - hit.span[0]), abs(position - hit.span[1]))
            if distance < best_distance:
                best, best_distance = hit, distance
        if best is not None and not best.has_time:
            best.at = parsed
            best.has_time = True
            best.tzoffset_minutes = _zone_near(text, match.end())
            best.text = f"{best.text} {match.group(0)}".strip()


def _zone_near(text: str, position: int, window: int = 24) -> int | None:
    """A timezone named just after a clock time, e.g. "10:00 CET" or "14:00 UTC+2"."""
    tail = text[position : position + window]
    match = _TZ_TOKEN.search(tail)
    if match is None:
        return None
    if match.group(3):
        return TZ_OFFSETS.get(match.group(3).lower())
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    total = hours * 60 + (minutes if hours >= 0 else -minutes)
    return total if -14 * 60 <= total <= 14 * 60 else None


def _parse_time(match: re.Match[str]) -> time | None:
    if match.group(1) is not None:
        hour, minute = int(match.group(1)), int(match.group(2))
        marker = (match.group(3) or "").lower()
    else:
        hour, minute = int(match.group(4)), 0
        marker = (match.group(5) or "").lower()
    if marker.startswith("p") and hour < 12:
        hour += 12
    if marker.startswith("a") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _cued_date(text: str, cue: re.Pattern[str], reference: date | None) -> DateHit | None:
    """The date nearest after a cue word ('deadline', 'Frist', 'interview')."""
    hits = find_dates(text, reference=reference)
    if not hits:
        return None
    best: tuple[int, DateHit] | None = None
    for match in cue.finditer(text):
        for hit in hits:
            distance = hit.span[0] - match.end()
            if -40 <= distance <= 200:
                score = abs(distance)
                if best is None or score < best[0]:
                    best = (score, hit)
    return best[1] if best else None


def find_deadline(
    text: str, *, reference: date | None = None, fallback: bool = False
) -> DateHit | None:
    """A date introduced by a deadline cue ("deadline", "Frist", "bis zum").

    `fallback=True` additionally accepts the latest future date in the text when
    no cue is present. That is off by default: an uncued date is far more often
    a meeting, a signature line or a reference number than a deadline, and a
    wrong deadline is worse than a missing one.
    """
    cued = _cued_date(text, _DEADLINE_CUE, reference)
    if cued or not fallback:
        return cued
    reference = reference or datetime.now(timezone.utc).date()
    future = [h for h in find_dates(text, reference=reference) if h.value >= reference]
    return max(future, key=lambda h: h.value) if future else None


def find_event(text: str, *, reference: date | None = None) -> DateHit | None:
    """A date/time introduced by a meeting cue; prefers hits that carry a clock time."""
    hits = find_dates(text, reference=reference)
    timed = [hit for hit in hits if hit.has_time]
    cued = _cued_date(text, _EVENT_CUE, reference)
    if cued and cued.has_time:
        return cued
    if timed:
        return timed[0]
    return cued
