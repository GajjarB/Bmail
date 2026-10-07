"""Stage 1 — everything that can be decided without a model.

Order matters. Templates run first because a recognised ATS mail is fully
resolved and must not be misfiled as bulk merely because it carries a
List-Unsubscribe header. Bulk detection runs next, then near-duplicate collapse,
and whatever survives is handed to Stage 2 as a candidate.

Realistically this resolves the majority of an inbox. Every message it settles
is a model call that never happens.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import timedelta, timezone

from bmail.models import Lane, Message, NextAction, Stage, Verdict
from bmail.packs import Pack
from bmail.rules.contacts import ContactSignals, contact_signals
from bmail.rules.headers import HeaderSignals, header_signals
from bmail.rules.templates import TemplateHit, match_template
from bmail.store import repo
from bmail.text.clean import normalise_subject
from bmail.text.dates import find_deadline, find_event
from bmail.text.simhash import hamming, simhash


@dataclass(slots=True)
class Stage1Result:
    verdict: Verdict
    resolved: bool                    # True = no further stage needed
    headers: HeaderSignals = field(default_factory=HeaderSignals)
    contacts: ContactSignals = field(default_factory=ContactSignals)
    template: TemplateHit | None = None
    duplicate_of: str | None = None


class RulesEngine:
    def __init__(
        self,
        pack: Pack,
        *,
        recipe_dir: str | None = None,
        dedupe_radius: int = 3,
    ) -> None:
        self.pack = pack
        self.recipe_dir = recipe_dir
        self.dedupe_radius = dedupe_radius
        self._seen: dict[int, tuple[str, Verdict]] = {}  # simhash -> first (id, verdict) this run
        # When set, a stored verdict older than this is not copied by a repeat:
        # see Pipeline.triage(fresh=True).
        self.trust_since: str | None = None

    # -- main entry point ---------------------------------------------------- #

    def run(self, conn: sqlite3.Connection, message: Message) -> Stage1Result:
        started = time.perf_counter()
        verdict = Verdict(message_id=message.message_id, pack=self.pack.name, stage=Stage.rules)

        headers = header_signals(message)
        contacts = contact_signals(conn, message)
        result = Stage1Result(verdict=verdict, resolved=False, headers=headers, contacts=contacts)

        template = match_template(message, self.recipe_dir)
        if template is not None and template.recipe.category in self.pack.categories:
            self._apply_template(message, verdict, template)
            # A job board's mailing has the same unsubscribe footer as any
            # newsletter; what it carries is jobs.
            if (verdict.category == "newsletter" and "job_alert" in self.pack.categories
                    and self.is_job_board_mail(message)):
                verdict.category = "job_alert"
                verdict.reasons.append("a job board's mailing: filed with job alerts")
            result.template = template
            result.resolved = True
        elif (duplicate := self._duplicate_of(conn, message)) is not None:
            original_id, original = duplicate
            # Fold, do not discard: a duplicate inherits the verdict of the message
            # it repeats, so collapsing forty identical acknowledgements does not
            # throw forty categories away.
            verdict.category = original.category
            verdict.action_required = original.action_required
            verdict.urgency = original.urgency
            verdict.next_action = original.next_action
            verdict.template_id = original.template_id
            verdict.needs_review = original.needs_review
            verdict.org = original.org
            verdict.role = original.role
            verdict.lane = Lane.duplicate
            verdict.confidence = min(0.95, original.confidence)
            verdict.summary = original.summary or _first_line(message)
            verdict.reasons.append(f"near-duplicate of {original_id}")
            result.duplicate_of = original_id
            result.resolved = True
        elif self._is_bulk(headers, contacts):
            verdict.lane = Lane.bulk
            verdict.category = self.category_for_machine_mail(message, headers)
            verdict.confidence = round(0.6 + 0.4 * headers.score, 2)
            verdict.summary = _first_line(message)
            verdict.reasons.extend(headers.reasons)
            result.resolved = True
        else:
            verdict.lane = Lane.candidate
            verdict.reasons.append(
                f"contact familiarity {contacts.familiarity:.1f}, "
                f"machine score {headers.score:.2f}"
            )

        self._apply_pack_defaults(verdict)
        self._extract_dates(message, verdict)
        if result.resolved and verdict.lane is not Lane.duplicate:
            self.remember(message, verdict)
        verdict.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    def remember(self, message: Message, verdict: Verdict) -> None:
        """Register a *final* verdict as the reference for later near-duplicates.

        Stage 1 calls this for the messages it settles itself. The pipeline calls
        it again once Stage 2/3 has finished a candidate, so that a repeat of a
        message the model classified inherits the model's answer instead of an
        empty one — folding must never lose a category.
        """
        fingerprint = self._fingerprint(message)
        if fingerprint:
            self._seen[fingerprint] = (message.message_id, verdict)

    # -- pieces --------------------------------------------------------------- #

    def _apply_template(self, message: Message, verdict: Verdict, hit: TemplateHit) -> None:
        recipe = hit.recipe
        verdict.lane = Lane.template
        verdict.category = recipe.category
        verdict.confidence = recipe.confidence
        verdict.template_id = recipe.id
        verdict.model = None
        verdict.reasons.append(f"template recipe '{recipe.id}' ({recipe.vendor or 'generic'})")
        verdict.org = hit.fields.get("org") or verdict.org
        verdict.role = hit.fields.get("role") or verdict.role
        verdict.contact_person = hit.fields.get("contact_person") or verdict.contact_person
        verdict.amount = hit.fields.get("amount") or verdict.amount
        verdict.org_domain = message.sender_domain
        verdict.summary = verdict.summary or _first_line(message)
        if recipe.action_required is not None:
            verdict.action_required = recipe.action_required

    def _is_bulk(self, headers: HeaderSignals, contacts: ContactSignals) -> bool:
        # A machine-looking message from someone you actually correspond with is
        # not bulk — that is how booking confirmations and portal notices arrive.
        if contacts.known_address and headers.score < 0.6:
            return False
        return headers.score >= 0.45

    # Everything these send is about jobs.
    _JOB_BOARDS = (
        "indeed.com", "indeedemail.com", "stepstone.de", "stepstone.com", "glassdoor.com",
        "glassdoor.de", "monster.de", "monster.com", "jobware.de", "kimeta.de",
        "jobs2web.com", "arbeitsagentur.de", "jobs.ch", "karriere.at",
    )
    # These also send posts, people and events: only their job mail is an alert.
    _SOCIAL_BOARDS = ("linkedin.com", "xing.com")
    # What an alert says in its subject; an article about getting a job does not.
    _JOB_SUBJECT = re.compile(
        r"\b(?:job alerts?|jobs? (?:near|in|for you|matching)|\d+\s+(?:new\s+)?jobs|new jobs|"
        r"weitere jobs|neue jobs|is hiring|are hiring|hiring for|stellenangebot\w*|stellenanzeige\w*|"
        r"looking for candidates|candidates like you|invitation to apply|reminder to apply|"
        r"jetzt bewerben)\b",
        re.IGNORECASE,
    )

    @classmethod
    def is_job_board_mail(cls, message: Message) -> bool:
        domain = message.sender_domain
        if any(domain == board or domain.endswith(f".{board}") for board in cls._JOB_BOARDS):
            return True
        if any(domain == board or domain.endswith(f".{board}") for board in cls._SOCIAL_BOARDS):
            # jobalerts-noreply@linkedin.com, jobs@mail.xing.com, jobs-noreply@...
            return "job" in message.from_.address.lower()
        return bool(cls._JOB_SUBJECT.search(message.subject))

    def category_for_machine_mail(self, message: Message, headers: HeaderSignals) -> str:
        """Give mail a machine sent the most specific category the pack declares."""
        preferences: list[str] = []
        if self.is_job_board_mail(message):
            preferences.append("job_alert")
        if headers.is_list:
            preferences.append("newsletter")
        if headers.is_automated or headers.is_noreply:
            preferences.append("admin")
        if headers.is_system and not headers.is_bulk:
            # service@, billing@, orders@: a shop or a bank about your account.
            preferences.append("admin")
        preferences.extend(("newsletter", "admin", "other"))
        for candidate in preferences:
            if candidate in self.pack.categories:
                return candidate
        return "other"

    def _duplicate_of(
        self, conn: sqlite3.Connection, message: Message
    ) -> tuple[str, Verdict] | None:
        fingerprint = self._fingerprint(message)
        if not fingerprint:
            return None
        for seen_hash, (seen_id, seen_verdict) in self._seen.items():
            if seen_id != message.message_id and hamming(seen_hash, fingerprint) <= self.dedupe_radius:
                return seen_id, seen_verdict
        row = conn.execute(
            """
            SELECT m.message_id, m.simhash FROM messages m
            JOIN verdicts v ON v.message_id = m.message_id AND v.pack = ?
            WHERE m.message_id != ? AND m.simhash != 0 AND m.from_domain = ?
              AND (v.stage = 'manual' OR v.created_at >= ?)
            ORDER BY m.date_utc DESC LIMIT 200
            """,
            (self.pack.name, message.message_id, message.sender_domain, self.trust_since or ""),
        ).fetchall()
        for candidate in row:
            if hamming(candidate["simhash"], fingerprint) <= self.dedupe_radius:
                stored = repo.get_verdict(conn, candidate["message_id"], self.pack.name)
                if stored is not None and _settled(stored):
                    self._seen[fingerprint] = (candidate["message_id"], stored)
                    return candidate["message_id"], stored
        return None

    @staticmethod
    def _fingerprint(message: Message) -> int:
        return simhash(f"{normalise_subject(message.subject)}\n{message.body_text}")

    def _apply_pack_defaults(self, verdict: Verdict) -> None:
        """Categories carry their own policy; the pack decides, not the engine."""
        spec = self.pack.spec(verdict.category)
        if verdict.lane in (Lane.template, Lane.bulk):
            verdict.action_required = spec.action_required
            verdict.urgency = spec.urgency
            verdict.next_action = spec.next_action

    def _extract_dates(self, message: Message, verdict: Verdict) -> None:
        """Deterministic date extraction, with the source sentence as evidence."""
        if verdict.lane is Lane.bulk:
            return
        text = f"{message.subject}\n{message.body_text}"
        reference = message.date.date()
        spec = self.pack.spec(verdict.category)

        if verdict.event_datetime is None and spec.next_action is NextAction.schedule:
            event = find_event(text, reference=reference)
            if event and event.has_time:
                # No zone named in the text: fall back to the offset on the
                # message's own Date header, which is the sender's local time
                # and a far better guess than UTC.
                fallback = message.date.utcoffset() or timedelta(0)
                verdict.event_datetime = event.as_utc(
                    default_tz=timezone(fallback)
                )
                verdict.evidence = verdict.evidence or _sentence_around(text, event.span)
        if verdict.deadline is None:
            deadline = find_deadline(text, reference=reference)
            if deadline:
                verdict.deadline = deadline.value
                verdict.evidence = verdict.evidence or _sentence_around(text, deadline.span)
        if verdict.evidence:
            verdict.evidence_ok = _normalise(verdict.evidence) in _normalise(
                message.evidence_haystack()
            )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


_GREETING = re.compile(
    r"^(dear|hi|hello|hey|good (morning|afternoon|evening)|hallo|liebe[rs]?|"
    r"sehr geehrte[rs]?|guten (tag|morgen|abend)|moin|servus)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[^\W\d_]{2,}")
_BARE_LINK = re.compile(r"^\(?\s*(?:https?://)?[\w.-]+\.[a-z]{2,}(?:/\S*)?\s*\)?$", re.IGNORECASE)


def _first_line(message: Message, limit: int = 120) -> str:
    """The first sentence that says something: the summary under a Brief entry.

    It used to be the first line over fifteen characters, which in formal mail
    is the greeting — "Sehr geehrte Bewerberin, sehr geehrter Bewerber," told
    you nothing about what the university wanted.
    """
    for line in message.body_text.split("\n"):
        stripped = " ".join(line.split())
        # "Hi Anna, could you send…" keeps what follows the greeting; a line
        # that is only greetings ("Sehr geehrte Bewerberin, sehr geehrter
        # Bewerber,") is skipped whole.
        while _GREETING.match(stripped):
            head, _, rest = stripped.partition(",")
            if len(head) >= 60:
                break
            stripped = rest.strip()
        # A member number or a bare link is not a sentence, however long.
        if len(_WORD.findall(stripped)) < 2 or _BARE_LINK.match(stripped):
            continue
        if len(stripped) > 15:
            # A short opening sentence rarely carries the ask; take the next
            # one too while there is room.
            summary = ""
            for sentence in _SENTENCE_END.split(stripped):
                if summary and (len(summary) >= 50 or len(summary) + len(sentence) >= limit):
                    break
                summary = f"{summary} {sentence}".strip()
            return _clip(summary, limit)
    return _clip(message.subject or "", limit)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def _sentence_around(text: str, span: tuple[int, int]) -> str:
    """The sentence containing `span` — the evidence the UI shows the user."""
    start = max(text.rfind(".", 0, span[0]), text.rfind("\n", 0, span[0]),
                text.rfind("!", 0, span[0]), text.rfind("?", 0, span[0]))
    start = 0 if start < 0 else start + 1
    ends = [pos for pos in (text.find(". ", span[1]), text.find("\n", span[1]),
                            text.find("! ", span[1]), text.find("? ", span[1])) if pos != -1]
    end = min(ends) + 1 if ends else min(len(text), span[1] + 160)
    return " ".join(text[start:end].split())[:300].strip()


def _normalise(text: str) -> str:
    return " ".join((text or "").split()).lower()


def resolve_all(
    conn: sqlite3.Connection, pack: Pack, messages: list[Message], recipe_dir: str | None = None
) -> list[Stage1Result]:
    engine = RulesEngine(pack, recipe_dir=recipe_dir)
    return [engine.run(conn, message) for message in messages]


def coverage(results: list[Stage1Result]) -> dict[str, float | int]:
    """How much of the corpus Stage 1 settled — the number that decides how much LLM you need."""
    total = len(results) or 1
    resolved = sum(1 for r in results if r.resolved)
    return {
        "total": len(results),
        "resolved": resolved,
        "templates": sum(1 for r in results if r.template is not None),
        "bulk": sum(1 for r in results if r.verdict.lane is Lane.bulk),
        "duplicates": sum(1 for r in results if r.verdict.lane is Lane.duplicate),
        "candidates": sum(1 for r in results if not r.resolved),
        "resolved_fraction": round(resolved / total, 3),
    }


def _settled(verdict: Verdict) -> bool:
    """Whether a verdict from an earlier run is safe for a repeat to copy.

    A human correction or a confident answer, yes. An unsure guess still
    waiting in the review queue, no: copying it made every repeat inherit the
    mistake, and because the store was consulted on every run, re-sorting
    could never clear it — each message found the other's stale answer. A
    repeat of an unsure message is classified in its own right instead, which
    costs a millisecond.
    """
    return verdict.stage is Stage.manual or not verdict.needs_review
