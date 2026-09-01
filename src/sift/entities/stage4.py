"""Stage 4 — entity resolution and the state machine.

This is the stage that makes Sift a tracker rather than a summariser. A
spreadsheet can hold rows; it cannot dedupe them, cannot hold a state machine,
and cannot tell you what has gone quiet. All three live here.

Ghosting detection is the headline output, and it is a feature rather than a
bug report: *"these fourteen have been silent for three weeks; six are worth one
follow-up"* is the sentence a job seeker actually wants, and nothing else in
this category produces it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from sift.entities.resolve import (
    GENERIC_MAIL_DOMAINS,
    case_key,
    is_ats_domain,
    keys_match,
    org_from_domain,
    org_key,
)
from sift.models import Case, CaseEvent, Direction, Message, NextAction, Verdict
from sift.packs import Pack
from sift.store import repo


@dataclass(slots=True)
class Stage4Result:
    case: Case | None
    created: bool = False
    transitioned: bool = False
    from_state: str | None = None
    to_state: str | None = None


class CaseResolver:
    def __init__(self, pack: Pack, *, fuzzy_threshold: int = 85) -> None:
        self.pack = pack
        self.fuzzy_threshold = fuzzy_threshold

    # -- keying ---------------------------------------------------------------- #

    def derive_org(self, message: Message, verdict: Verdict) -> tuple[str, str]:
        """Return (display name, organisation key). Both are "" when unknown.

        A mail from `no-reply@greenhouse.io` is not a mail from Greenhouse, and
        one from `jonas@gmx.de` is not a mail from GMX. When the sender is a
        sending platform or a consumer mailbox, the only thing that identifies
        the organisation is the extracted name or the display name — and if
        neither is there, the honest answer is that there is no organisation.
        """
        from sift.rules.templates import _strip_via

        domain = message.sender_domain
        extracted = (verdict.org or "").strip()
        if domain in GENERIC_MAIL_DOMAINS:
            # gmail/gmx/web.de: the display name is a person. Only a name the
            # extraction stage actually found in the text counts as an employer.
            return extracted, org_key(extracted)
        if is_ats_domain(domain):
            display = extracted or _strip_via(message.from_.name)
            return display, org_key(display)
        display = extracted or org_from_domain(domain).replace("-", " ").title()
        return display, org_key(display) or org_key(domain)

    def build_key(self, message: Message, verdict: Verdict) -> tuple[str, str]:
        display, key_domain = self.derive_org(message, verdict)
        values = {
            "org": key_domain,
            "org_domain": key_domain,
            "company": key_domain,
            "company_domain": key_domain,
            "role": verdict.role or "",
            "role_title": verdict.role or "",
            "programme": verdict.role or "",
            "contact_person": verdict.contact_person or message.from_.address,
        }
        return case_key(self.pack.key, values), display

    # -- resolution -------------------------------------------------------------- #

    def resolve(
        self, conn: sqlite3.Connection, message: Message, verdict: Verdict
    ) -> Stage4Result:
        spec = self.pack.spec(verdict.category)
        existing = self._by_thread(conn, message)
        key, display = self.build_key(message, verdict)

        if existing is None:
            existing = self._by_key(conn, key)
        if existing is None:
            if not (spec.opens_case or spec.state):
                # Newsletters and admin noise must never mint a case.
                return Stage4Result(case=None)
            if verdict.needs_review or not key.split("|")[0]:
                # Two guards against junk cases, and both matter in practice:
                # an uncertain classification should not mint an entity, and
                # neither should a message with no identifiable organisation.
                # Attaching to an existing case is still fine — only creation
                # is blocked.
                return Stage4Result(case=None)
            existing = Case(
                pack=self.pack.name,
                key=key,
                title=self._title(display, verdict),
                org=display,
                org_domain=message.sender_domain,
                role=verdict.role,
                state=self.pack.initial_state,
                opened_at=message.date,
            )
            created = True
        else:
            created = False

        return self._apply(conn, existing, message, verdict, created)

    def _by_thread(self, conn: sqlite3.Connection, message: Message) -> Case | None:
        """Follow the References chain — the most reliable link available."""
        candidates = [message.thread_id, message.in_reply_to, *message.references]
        for reference in [c for c in candidates if c]:
            case = repo.find_case_by_message(conn, self.pack.name, reference)
            if case is not None:
                return case
        row = conn.execute(
            """
            SELECT c.* FROM cases c
            JOIN case_messages cm ON cm.case_id = c.id
            JOIN messages m ON m.message_id = cm.message_id
            WHERE c.pack = ? AND m.thread_id = ? LIMIT 1
            """,
            (self.pack.name, message.thread_id or ""),
        ).fetchone()
        return repo.row_to_case(row) if row else None

    def _by_key(self, conn: sqlite3.Connection, key: str) -> Case | None:
        exact = repo.find_case(conn, self.pack.name, key)
        if exact is not None:
            return exact
        organisation = key.split("|")[0]
        if not organisation:
            return None
        rows = conn.execute(
            "SELECT * FROM cases WHERE pack = ? AND key LIKE ?",
            (self.pack.name, f"{organisation}|%"),
        ).fetchall()
        for row in rows:
            if keys_match(key, row["key"], threshold=self.fuzzy_threshold):
                return repo.row_to_case(row)
        return None

    # -- state ------------------------------------------------------------------- #

    def _apply(
        self,
        conn: sqlite3.Connection,
        case: Case,
        message: Message,
        verdict: Verdict,
        created: bool,
    ) -> Stage4Result:
        spec = self.pack.spec(verdict.category)
        previous = case.state
        target = spec.state

        if target and self._may_transition(previous, target):
            case.state = target
        if spec.closes_case or case.state in self.pack.terminal_states:
            case.closed = True

        case.last_event_at = _latest(case.last_event_at, message.date)
        if message.direction is Direction.outbound:
            case.last_outbound_at = _latest(case.last_outbound_at, message.date)
        else:
            case.last_inbound_at = _latest(case.last_inbound_at, message.date)
        case.opened_at = case.opened_at or message.date
        case.org = case.org or verdict.org
        case.role = case.role or verdict.role
        case.contact_person = case.contact_person or verdict.contact_person
        if verdict.event_datetime:
            case.event_datetime = verdict.event_datetime
        if verdict.deadline and (
            case.next_action_due is None or verdict.deadline < case.next_action_due
        ):
            case.next_action_due = verdict.deadline
        if verdict.action_required and verdict.next_action is not NextAction.none:
            case.next_action = verdict.next_action
        elif case.closed:
            case.next_action = NextAction.none
        if not case.title:
            case.title = self._title(case.org or "", verdict)

        stored = repo.save_case(conn, case)
        if stored.id is None:  # pragma: no cover - defensive
            return Stage4Result(case=None)
        repo.link_message(conn, stored.id, message.message_id)
        stored.message_count = len(repo.case_message_ids(conn, stored.id))
        stored = repo.save_case(conn, stored)

        transitioned = stored.state != previous
        if created or transitioned:
            repo.add_case_event(
                conn,
                CaseEvent(
                    case_id=stored.id,
                    message_id=message.message_id,
                    at=message.date,
                    from_state=None if created else previous,
                    to_state=stored.state,
                    category=verdict.category,
                    note="case opened" if created else f"{previous} -> {stored.state}",
                ),
            )
        return Stage4Result(
            case=stored,
            created=created,
            transitioned=transitioned,
            from_state=previous,
            to_state=stored.state,
        )

    def _may_transition(self, current: str, target: str) -> bool:
        """States move forward. A late acknowledgement must not undo an offer."""
        order = self.pack.states
        if current not in order or target not in order:
            return True
        if current in self.pack.terminal_states:
            return False
        return order.index(target) >= order.index(current)

    def _title(self, org: str, verdict: Verdict) -> str:
        role = verdict.role or ""
        return f"{org} — {role}".strip(" —") if org or role else "Untitled case"


# --------------------------------------------------------------------------- #
# Derived signals
# --------------------------------------------------------------------------- #


def refresh_derived(
    conn: sqlite3.Connection, pack: Pack, *, now: datetime | None = None
) -> dict[str, int]:
    """Recompute time-based state: stalls, and any pack-declared timed transition.

    Time is the one input no message carries, so these transitions cannot be
    driven by classification. They are recomputed on every triage run.
    """
    now = now or datetime.now(timezone.utc)
    stalled = transitioned = 0
    for case in repo.list_cases(conn, pack.name, open_only=True):
        silent = case.days_silent(now)
        if silent is None:
            continue
        was_stalled = case.stalled
        case.stalled = silent >= pack.stall_days and case.state not in pack.terminal_states
        for target, rule in pack.transitions.items():
            if case.state in rule.from_states and silent >= rule.after_days_silent:
                if case.state != target:
                    previous = case.state
                    case.state = target
                    transitioned += 1
                    repo.add_case_event(
                        conn,
                        CaseEvent(
                            case_id=case.id or 0,
                            at=now,
                            from_state=previous,
                            to_state=target,
                            note=f"no reply for {silent} days",
                        ),
                    )
                break
        if case.stalled and not was_stalled:
            stalled += 1
        repo.save_case(conn, case)
    conn.commit()
    return {"stalled": stalled, "transitioned": transitioned}


def _latest(current: datetime | None, candidate: datetime) -> datetime:
    if current is None:
        return candidate
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(current, candidate)
