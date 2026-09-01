"""The Brief — one screen, generated on a schedule.

This is the product surface that decides whether the whole thing is worth
running. If the top five items each morning are not right, no macro-F1 saves it,
which is why `precision@k` on this list is a headline metric in the evaluation
harness rather than an afterthought.

Ranking is deliberately simple and explainable: urgency, then a deadline that is
close, then whether an action is required, then recency. Anything a user cannot
predict, they cannot trust.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from sift.models import Brief, BriefItem, Urgency
from sift.packs import Pack
from sift.store import repo

_URGENCY_WEIGHT = {
    Urgency.today: 100.0,
    Urgency.this_week: 60.0,
    Urgency.whenever: 20.0,
    Urgency.none: 0.0,
}


def _score(verdict, now: datetime) -> float:
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
    age_days = max(0.0, (now - verdict.created_at).days)
    return score - min(age_days, 14) * 1.5


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

    scored: list[tuple[float, BriefItem]] = []
    archived = 0
    for verdict, row in repo.iter_verdicts(conn, pack.name, since=since):
        title = row["subject"] or "(no subject)"
        sender = row["from_name"] or row["from_addr"]
        if not verdict.action_required and verdict.urgency is Urgency.none:
            archived += 1
            if len(brief.archived) < 20:
                brief.archived.append(
                    BriefItem(
                        kind="archived",
                        title=title,
                        detail=f"{sender} · {verdict.category}",
                        message_id=verdict.message_id,
                    )
                )
            continue
        item = BriefItem(
            kind="needs_you",
            title=title,
            detail=f"{sender} · {verdict.category}"
            + (f" · {verdict.summary}" if verdict.summary else ""),
            when=verdict.event_datetime,
            due=verdict.deadline,
            message_id=verdict.message_id,
            evidence=verdict.evidence,
        )
        item.score = _score(verdict, now)
        scored.append((item.score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    brief.needs_you = [item for _, item in scored[:limit]]

    horizon = now.date() + timedelta(days=14)
    for case in repo.list_cases(conn, pack.name, open_only=True):
        if case.next_action_due and case.next_action_due <= horizon:
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
            brief.stalled.append(
                BriefItem(
                    kind="stalled",
                    title=case.title or case.key,
                    detail=f"silent {silent} days · {case.state}",
                    case_id=case.id,
                    score=float(silent),
                )
            )

    brief.deadlines.sort(key=lambda item: item.due or date.max)
    brief.events.sort(key=lambda item: item.when or datetime.max.replace(tzinfo=timezone.utc))
    brief.stalled.sort(key=lambda item: item.score, reverse=True)

    open_cases = repo.list_cases(conn, pack.name, open_only=True)
    brief.counts = {
        "needs_you": len(brief.needs_you),
        "deadlines": len(brief.deadlines),
        "events": len(brief.events),
        "stalled": len(brief.stalled),
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


def render_text(brief: Brief) -> str:
    counts = brief.counts
    lines = [
        f"Sift brief · {brief.generated_at.strftime('%a %d %b %Y %H:%M UTC')} · pack: {brief.pack}",
        "=" * 72,
        f"{counts.get('needs_you', 0)} need you · {counts.get('deadlines', 0)} deadlines "
        f"· {counts.get('events', 0)} upcoming · {counts.get('stalled', 0)} stalled "
        f"· {counts.get('archived', 0)} archived",
        "",
    ]
    if brief.needs_you:
        lines.append("NEEDS YOU")
        for index, item in enumerate(brief.needs_you, start=1):
            lines.append(f" {index}. {item.title}")
            lines.append(f"    {item.detail}")
            if item.due:
                lines.append(f"    deadline {item.due.isoformat()}")
            if item.when:
                lines.append(f"    {item.when.strftime('%a %d %b %H:%M UTC')}")
            if item.evidence:
                lines.append(f'    evidence: "{item.evidence[:140]}"')
        lines.append("")
    for heading, items in (
        ("DEADLINES (next 14 days)", brief.deadlines),
        ("UPCOMING", brief.events),
        ("STALLED — worth one follow-up", brief.stalled),
    ):
        if items:
            lines.append(heading)
            lines.extend(f" · {item.title} — {item.detail}" for item in items[:15])
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
        f"**{counts.get('needs_you', 0)} need you today** · "
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
            lines.append(f"  {item.detail}")
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
            lines += [f"- {item.title} — {item.detail}" for item in items[:20]]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
