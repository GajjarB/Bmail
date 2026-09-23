"""The Brief — one screen, generated on a schedule.

This is the product surface that decides whether the whole thing is worth
running. If the top five items each morning are not right, no macro-F1 saves it,
which is why `precision@k` on this list is a headline metric in the evaluation
harness rather than an afterthought.

Ranking is deliberately simple and explainable: urgency, then a deadline that is
close, then whether an action is required, then recency. Anything a user cannot
predict, they cannot trust.

"Needs you" means *still open*, not *arrived lately*. It used to be every
request that arrived inside the window — a day, by default — so an interview
invitation you had not answered vanished the next morning. A request now stays
until something settles it: you replied in its thread, a later mail on the same
case moved it on, its event has happened, or its deadline has gone stale. One
with no date at all ages out after `OPEN_FOR_DAYS`. The window still decides
which arrivals are listed as filed.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from bmail.models import Brief, BriefItem, Urgency
from bmail.packs import Pack
from bmail.store import repo

# How long an overdue deadline stays worth showing before it is just old.
STALE_DEADLINE_DAYS = 30
# How long a request with no deadline and no date stays open unanswered.
OPEN_FOR_DAYS = 30
# Past this much silence a follow-up is not worth sending; the case is simply
# quiet. It still counts as stalled, but the Brief stops suggesting it.
FOLLOW_UP_WITHIN_DAYS = 60

_URGENCY_WEIGHT = {
    Urgency.today: 100.0,
    Urgency.this_week: 60.0,
    Urgency.whenever: 20.0,
    Urgency.none: 0.0,
}


def _score(verdict, now: datetime, received: datetime | None = None) -> float:
    score = _URGENCY_WEIGHT.get(verdict.urgency, 0.0)
    if verdict.action_required:
        score += 40.0
    if verdict.deadline:
        days = (verdict.deadline - now.date()).days
        if days < 0:
            score += 30.0            # overdue still matters
        elif days <= 7:
            score += 45.0 - days * 4
    if verdict.event_datetime:
        hours = (verdict.event_datetime - now).total_seconds() / 3600
        if 0 <= hours <= 72:
            score += 35.0
    if verdict.needs_review:
        score += 10.0                # uncertain but plausibly important
    # Age from when the mail arrived. Using when it was sorted made everything
    # look brand new after a re-sort.
    age_days = max(0.0, (now - _utc(received or verdict.created_at)).days)
    return score - min(age_days, 14) * 1.5


_REPLY_PREFIX = re.compile(r"^\s*((re|aw|fwd?|wg|antw)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def _topic(subject: str | None) -> str:
    """A subject without its Re:/AW:/Fwd: chain, for grouping one conversation."""
    return " ".join(_REPLY_PREFIX.sub("", subject or "").lower().split())


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _still_open(verdict, row, now: datetime, replies: dict[str, datetime]) -> bool:
    """Whether a request is still waiting on you. See the module docstring."""
    received = repo._text_to_dt(row["date_utc"])
    if received is None:
        return False
    received = _utc(received)
    thread = row["thread_id"]
    if thread and thread in replies and _utc(replies[thread]) > received:
        return False                                   # you answered it
    case_last = repo._text_to_dt(row["case_last_inbound"])
    if case_last is not None and _utc(case_last) > received:
        return False                                   # the case has moved on
    today = now.date()
    if verdict.deadline is not None:
        return verdict.deadline >= today - timedelta(days=STALE_DEADLINE_DAYS)
    if verdict.event_datetime is not None:
        return _utc(verdict.event_datetime) >= now     # a past interview is done
    return received >= now - timedelta(days=OPEN_FOR_DAYS)


def build_brief(
    conn: sqlite3.Connection,
    pack: Pack,
    *,
    window_days: int = 1,
    limit: int = 8,
    now: datetime | None = None,
) -> Brief:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    brief = Brief(generated_at=now, window_days=window_days, pack=pack.name)

    # The case knows the tidied organisation name; a lone message often does not.
    case_labels = repo.case_labels_for_messages(conn, pack.name)

    # Filed: what arrived inside the window and asked for nothing.
    archived = 0
    for verdict, row in repo.iter_verdicts(conn, pack.name, since=since):
        if verdict.action_required or verdict.urgency is not Urgency.none:
            continue
        archived += 1
        if len(brief.archived) < 20:
            brief.archived.append(
                BriefItem(
                    kind="archived",
                    title=row["subject"] or "(no subject)",
                    detail=f"{row['from_name'] or row['from_addr']} · {verdict.category}",
                    message_id=verdict.message_id,
                )
            )

    # Needs you: every request still open, however long ago it arrived — one
    # per conversation, the newest. A reminder and the mail it reminds you of
    # are one thing to do, and so are ten copies of the same message.
    replies = repo.last_reply_by_thread(conn)
    newest: dict[object, tuple] = {}
    for verdict, row in repo.open_item_candidates(conn, pack.name):
        # Outside a case, a conversation is who asked and about what: copies
        # and replies of one message often carry different thread ids.
        key = (("case", row["case_id"]) if row["case_id"] is not None
               else ("mail", (row["from_addr"] or "").lower(), _topic(row["subject"])))
        if key in newest:                     # candidates arrive newest first
            continue
        newest[key] = (verdict, row)

    scored: list[tuple[float, BriefItem]] = []
    for verdict, row in newest.values():
        if not _still_open(verdict, row, now, replies):
            continue
        case_org, case_role = case_labels.get(verdict.message_id, (None, None))
        item = BriefItem(
            kind="needs_you",
            title=row["subject"] or "(no subject)",
            detail=verdict.summary or row["from_name"] or row["from_addr"],
            org=case_org or verdict.org or row["from_name"] or row["from_addr"],
            # The message's own role wins over the case's. A case can hold an
            # older or blanker title than the mail in front of you, and showing
            # the wrong role next to a real subject line reads as a bug.
            role=verdict.role or case_role,
            category=verdict.category,
            urgency=verdict.urgency,
            when=verdict.event_datetime,
            due=verdict.deadline,
            message_id=verdict.message_id,
            case_id=row["case_id"],
            evidence=verdict.evidence,
        )
        item.score = _score(verdict, now, repo._text_to_dt(row["date_utc"]))
        scored.append((item.score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    needing = len(scored)
    brief.needs_you = [item for _, item in scored[:limit]]

    # Overdue deadlines belong in the Brief — they are the most urgent thing
    # there is. Ancient ones do not: a date three months gone is stale data, not
    # a task, and showing it for ever is how a Brief starts crying wolf.
    quiet = 0
    horizon = now.date() + timedelta(days=14)
    floor = now.date() - timedelta(days=STALE_DEADLINE_DAYS)
    for case in repo.list_cases(conn, pack.name, open_only=True):
        if case.next_action_due and floor <= case.next_action_due <= horizon:
            brief.deadlines.append(
                BriefItem(
                    kind="deadline",
                    title=case.title or case.key,
                    detail=_due_phrase(case.next_action_due, now.date()),
                    due=case.next_action_due,
                    case_id=case.id,
                )
            )
        if case.event_datetime and now <= case.event_datetime <= now + timedelta(days=14):
            brief.events.append(
                BriefItem(
                    kind="event",
                    title=case.title or case.key,
                    detail=case.event_datetime.strftime("%a %d %b, %H:%M UTC"),
                    when=case.event_datetime,
                    case_id=case.id,
                )
            )
        if case.stalled:
            silent = case.days_silent(now) or 0
            quiet += 1
            if silent > FOLLOW_UP_WITHIN_DAYS:
                continue                      # long gone quiet: counted, not suggested
            brief.stalled.append(
                BriefItem(
                    kind="stalled",
                    title=case.title or case.key,
                    detail=f"silent {silent} days · {case.state}",
                    case_id=case.id,
                    score=float(silent),
                )
            )

    # A request with a deadline is a deadline, whether or not a case holds it.
    # Counting only case deadlines put "0 deadlines" over two overdue ones.
    listed = {item.case_id for item in brief.deadlines}
    for _, item in scored:
        if item.due and floor <= item.due <= horizon and (
            item.case_id is None or item.case_id not in listed
        ):
            brief.deadlines.append(
                BriefItem(
                    kind="deadline",
                    title=item.title,
                    detail=_due_phrase(item.due, now.date()),
                    due=item.due,
                    case_id=item.case_id,
                    message_id=item.message_id,
                )
            )
            listed.add(item.case_id)
    brief.deadlines.sort(key=lambda item: item.due or date.max)
    brief.events.sort(key=lambda item: item.when or datetime.max.replace(tzinfo=timezone.utc))
    # The freshest silence first: that is the follow-up most likely to land.
    brief.stalled.sort(key=lambda item: item.score)

    open_cases = repo.list_cases(conn, pack.name, open_only=True)
    brief.counts = {
        # The true number, not the length of the truncated list: a count that
        # changes when you change the page size is not a count.
        "needs_you": needing,
        "showing": len(brief.needs_you),
        "deadlines": len(brief.deadlines),
        "events": len(brief.events),
        "stalled": len(brief.stalled),
        "quiet": quiet,
        "archived": archived,
        "open_cases": len(open_cases),
        "total_cases": len(repo.list_cases(conn, pack.name)),
    }
    return brief


def _due_phrase(due: date, today: date) -> str:
    days = (due - today).days
    if days < 0:
        return f"overdue by {-days} d ({due.isoformat()})"
    if days == 0:
        return f"due today ({due.isoformat()})"
    if days == 1:
        return f"due tomorrow ({due.isoformat()})"
    return f"due in {days} days ({due.isoformat()})"


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def _line(item: BriefItem) -> str:
    """The one-line context under a Brief entry, from the structured fields.

    Built here rather than stored on the item: the same item is rendered to a
    terminal, to Markdown and to a web page, and each wants it slightly
    differently.
    """
    parts = [part for part in (item.org, item.role, item.category.replace("_", " ")) if part]
    context = " · ".join(parts)
    if item.detail and item.detail not in parts:
        context = f"{context} · {item.detail}" if context else item.detail
    return context


def _needs(counts: dict) -> str:
    n = counts.get("needs_you", 0)
    return f"{n} needs you" if n == 1 else f"{n} need you"


def render_text(brief: Brief) -> str:
    counts = brief.counts
    lines = [
        f"Bmail brief · {brief.generated_at.strftime('%a %d %b %Y %H:%M UTC')} · pack: {brief.pack}",
        "=" * 72,
        f"{_needs(counts)} · {counts.get('deadlines', 0)} deadlines "
        f"· {counts.get('events', 0)} upcoming · {counts.get('stalled', 0)} stalled "
        f"· {counts.get('archived', 0)} archived",
        "",
    ]
    if brief.needs_you:
        lines.append("NEEDS YOU")
        for index, item in enumerate(brief.needs_you, start=1):
            lines.append(f" {index}. {item.title}")
            lines.append(f"    {_line(item)}")
            if item.due:
                lines.append(f"    deadline {item.due.isoformat()}")
            if item.when:
                lines.append(f"    {item.when.strftime('%a %d %b %H:%M UTC')}")
            if item.evidence:
                lines.append(f'    evidence: "{item.evidence[:140]}"')
        lines.append("")
    for heading, items in (
        ("DEADLINES", brief.deadlines),
        ("UPCOMING", brief.events),
        ("STALLED: worth one follow-up", brief.stalled),
    ):
        if items:
            lines.append(heading)
            lines.extend(f" · {item.title} · {item.detail}" for item in items[:15])
            lines.append("")
    if brief.archived:
        lines.append(f"ARCHIVED ({counts.get('archived', 0)}), most recent:")
        lines.extend(f" · {item.title[:70]}" for item in brief.archived[:10])
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(brief: Brief) -> str:
    counts = brief.counts
    lines = [
        f"# Brief — {brief.generated_at.strftime('%A %d %B %Y')}",
        "",
        f"**{_needs(counts)}** · "
        f"{counts.get('deadlines', 0)} deadlines · "
        f"{counts.get('stalled', 0)} stalled · "
        f"{counts.get('archived', 0)} archived",
        "",
    ]
    if brief.needs_you:
        lines += ["## Needs you", ""]
        for item in brief.needs_you:
            suffix = f" — _due {item.due.isoformat()}_" if item.due else ""
            lines.append(f"- **{item.title}**{suffix}  ")
            lines.append(f"  {_line(item)}")
            if item.evidence:
                lines.append(f"  > {item.evidence[:200]}")
        lines.append("")
    for heading, items in (
        ("Deadlines", brief.deadlines),
        ("Upcoming", brief.events),
        ("Stalled", brief.stalled),
    ):
        if items:
            lines += [f"## {heading}", ""]
            lines += [f"- {item.title} · {item.detail}" for item in items[:20]]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
