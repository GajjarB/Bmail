"""Every SQL statement in Bmail lives here. Nothing else touches the schema."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from typing import Any

from bmail.models import (
    Attachment,
    Case,
    CaseEvent,
    Direction,
    EmailAddress,
    Message,
    Verdict,
)
from bmail.text.simhash import simhash

# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #


def _dt_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text_to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _addrs_to_json(addresses: Sequence[EmailAddress]) -> str:
    return json.dumps([{"n": a.name, "a": a.address} for a in addresses], ensure_ascii=False)


def _json_to_addrs(raw: str | None) -> list[EmailAddress]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [EmailAddress(name=i.get("n", ""), address=i.get("a", "")) for i in items]


def row_to_message(row: sqlite3.Row, body: sqlite3.Row | None = None) -> Message:
    return Message(
        message_id=row["message_id"],
        provider=row["provider"],
        account=row["account"],
        uid=row["uid"],
        folder=row["folder"],
        thread_id=row["thread_id"],
        in_reply_to=row["in_reply_to"],
        references=json.loads(row["refs"] or "[]"),
        **{"from": EmailAddress(name=row["from_name"], address=row["from_addr"])},
        to=_json_to_addrs(row["to_addrs"]),
        cc=_json_to_addrs(row["cc_addrs"]),
        reply_to=EmailAddress(address=row["reply_to"]) if row["reply_to"] else None,
        date=_text_to_dt(row["date_utc"]) or datetime.now(timezone.utc),
        subject=row["subject"],
        list_id=row["list_id"],
        list_unsubscribe=row["list_unsubscribe"],
        precedence=row["precedence"],
        auto_submitted=row["auto_submitted"],
        x_mailer=row["x_mailer"],
        auth_results=row["auth_results"],
        return_path=row["return_path"],
        has_html=bool(row["has_html"]),
        language=row["language"],
        attachments=[Attachment(**a) for a in json.loads(row["attachments"] or "[]")],
        direction=Direction(row["direction"]),
        headers=json.loads(row["headers"] or "{}"),
        body_text=body["body_text"] if body else "",
        body_raw_text=body["body_raw_text"] if body else "",
    )


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


def upsert_message(conn: sqlite3.Connection, message: Message) -> bool:
    """Insert a message. Returns True if it was new."""
    from bmail.text.clean import normalise_subject

    existing = conn.execute(
        "SELECT 1 FROM messages WHERE message_id = ?", (message.message_id,)
    ).fetchone()
    fingerprint = simhash(f"{normalise_subject(message.subject)}\n{message.body_text}")
    now = _dt_to_text(datetime.now(timezone.utc))
    conn.execute(
        """
        INSERT INTO messages (
            message_id, provider, account, uid, folder, thread_id, in_reply_to, refs,
            from_name, from_addr, from_domain, to_addrs, cc_addrs, reply_to,
            date_utc, subject, subject_norm, list_id, list_unsubscribe, precedence,
            auto_submitted, x_mailer, auth_results, return_path, has_html, language,
            attachments, direction, headers, simhash, ingested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(message_id) DO UPDATE SET
            thread_id = excluded.thread_id,
            folder    = COALESCE(excluded.folder, messages.folder),
            language  = excluded.language,
            simhash   = excluded.simhash
        """,
        (
            message.message_id, message.provider, message.account, message.uid, message.folder,
            message.thread_id, message.in_reply_to, json.dumps(message.references),
            message.from_.name, message.from_.address, message.from_.domain,
            _addrs_to_json(message.to), _addrs_to_json(message.cc),
            message.reply_to.address if message.reply_to else None,
            _dt_to_text(message.date), message.subject, normalise_subject(message.subject),
            message.list_id, message.list_unsubscribe, message.precedence,
            message.auto_submitted, message.x_mailer, message.auth_results, message.return_path,
            int(message.has_html), message.language,
            json.dumps([a.model_dump() for a in message.attachments], ensure_ascii=False),
            message.direction.value, json.dumps(message.headers, ensure_ascii=False),
            fingerprint, now,
        ),
    )
    conn.execute(
        "INSERT INTO bodies(message_id, body_text, body_raw_text) VALUES (?,?,?) "
        "ON CONFLICT(message_id) DO UPDATE SET body_text = excluded.body_text, "
        "body_raw_text = excluded.body_raw_text",
        (message.message_id, message.body_text, message.body_raw_text),
    )
    conn.execute("DELETE FROM messages_fts WHERE message_id = ?", (message.message_id,))
    conn.execute(
        "INSERT INTO messages_fts(message_id, subject, body) VALUES (?,?,?)",
        (message.message_id, message.subject, message.body_text),
    )
    _touch_contacts(conn, message)
    return existing is None


def _touch_contacts(conn: sqlite3.Connection, message: Message) -> None:
    stamp = _dt_to_text(message.date)
    if message.direction is Direction.outbound:
        people, sent, received = message.recipients, 1, 0
    else:
        people, sent, received = [message.from_], 0, 1
    for person in people:
        if not person.address:
            continue
        conn.execute(
            """
            INSERT INTO contacts(address, domain, display_name, sent_count, received_count,
                                 first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(address) DO UPDATE SET
                sent_count     = contacts.sent_count + ?,
                received_count = contacts.received_count + ?,
                display_name   = CASE WHEN contacts.display_name = '' THEN excluded.display_name
                                      ELSE contacts.display_name END,
                first_seen     = MIN(contacts.first_seen, excluded.first_seen),
                last_seen      = MAX(contacts.last_seen, excluded.last_seen)
            """,
            (
                person.address.lower(), person.domain, person.name, sent, received,
                stamp, stamp, sent, received,
            ),
        )


def get_message(conn: sqlite3.Connection, message_id: str) -> Message | None:
    row = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
    if row is None:
        return None
    body = conn.execute("SELECT * FROM bodies WHERE message_id = ?", (message_id,)).fetchone()
    return row_to_message(row, body)


def iter_messages(
    conn: sqlite3.Connection,
    *,
    direction: Direction | None = Direction.inbound,
    since: datetime | None = None,
    limit: int | None = None,
    only_untriaged_for: str | None = None,
    account: str | None = None,
) -> list[Message]:
    sql = ["SELECT m.*, b.body_text, b.body_raw_text FROM messages m",
           "LEFT JOIN bodies b ON b.message_id = m.message_id"]
    where: list[str] = []
    params: list[Any] = []
    if direction is not None:
        where.append("m.direction = ?")
        params.append(direction.value)
    if since is not None:
        where.append("m.date_utc >= ?")
        params.append(_dt_to_text(since))
    if account:
        where.append("m.account = ?")
        params.append(account)
    if only_untriaged_for:
        where.append(
            "NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.message_id = m.message_id AND v.pack = ?)"
        )
        params.append(only_untriaged_for)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY m.date_utc DESC")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [row_to_message(row, row) for row in rows]


def count_messages(conn: sqlite3.Connection) -> dict[str, int]:
    total = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    inbound = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE direction = 'inbound'"
    ).fetchone()["c"]
    return {"total": total, "inbound": inbound, "outbound": total - inbound}


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    """Full-text search. Falls back to a quoted phrase when the query is not
    valid FTS5 syntax, so a stray quote or a trailing AND cannot crash the CLI."""
    try:
        return _search(conn, query, limit)
    except sqlite3.OperationalError:
        phrase = '"' + query.replace('"', '""') + '"'
        return _search(conn, phrase, limit)


def _search(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.message_id, m.subject, m.from_addr, m.date_utc,
               snippet(messages_fts, 2, '[', ']', '…', 12) AS snippet
        FROM messages_fts f
        JOIN messages m ON m.message_id = f.message_id
        WHERE messages_fts MATCH ?
        ORDER BY m.date_utc DESC LIMIT ?
        """,
        (query, limit),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Contact graph
# --------------------------------------------------------------------------- #


def contact_stats(conn: sqlite3.Connection, address: str, domain: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT sent_count, received_count FROM contacts WHERE address = ?", (address.lower(),)
    ).fetchone()
    domain_row = conn.execute(
        "SELECT COALESCE(SUM(sent_count),0) s, COALESCE(SUM(received_count),0) r "
        "FROM contacts WHERE domain = ?",
        (domain.lower(),),
    ).fetchone()
    return {
        "sent_to_address": row["sent_count"] if row else 0,
        "received_from_address": row["received_count"] if row else 0,
        "sent_to_domain": domain_row["s"] if domain_row else 0,
        "received_from_domain": domain_row["r"] if domain_row else 0,
    }


def near_duplicates(conn: sqlite3.Connection, fingerprint: int, radius: int = 3) -> list[str]:
    """Messages within `radius` Hamming distance. Exact-match indexed, rest scanned."""
    from bmail.text.simhash import hamming

    rows = conn.execute(
        "SELECT message_id, simhash FROM messages WHERE simhash != 0"
    ).fetchall()
    return [r["message_id"] for r in rows if hamming(r["simhash"], fingerprint) <= radius]


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def save_verdict(conn: sqlite3.Connection, verdict: Verdict) -> None:
    conn.execute(
        """
        INSERT INTO verdicts (
            message_id, pack, category, action_required, urgency, next_action, summary,
            org, org_domain, role, contact_person, event_datetime, deadline, amount,
            evidence, evidence_ok, confidence, stage, lane, model, template_id, reasons,
            needs_review, latency_ms, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(message_id, pack) DO UPDATE SET
            category = excluded.category, action_required = excluded.action_required,
            urgency = excluded.urgency, next_action = excluded.next_action,
            summary = excluded.summary, org = excluded.org, org_domain = excluded.org_domain,
            role = excluded.role, contact_person = excluded.contact_person,
            event_datetime = excluded.event_datetime, deadline = excluded.deadline,
            amount = excluded.amount, evidence = excluded.evidence,
            evidence_ok = excluded.evidence_ok, confidence = excluded.confidence,
            stage = excluded.stage, lane = excluded.lane, model = excluded.model,
            template_id = excluded.template_id, reasons = excluded.reasons,
            needs_review = excluded.needs_review, latency_ms = excluded.latency_ms,
            created_at = excluded.created_at
        """,
        (
            verdict.message_id, verdict.pack, verdict.category, int(verdict.action_required),
            verdict.urgency.value, verdict.next_action.value, verdict.summary,
            verdict.org, verdict.org_domain, verdict.role, verdict.contact_person,
            _dt_to_text(verdict.event_datetime),
            verdict.deadline.isoformat() if verdict.deadline else None,
            verdict.amount, verdict.evidence,
            None if verdict.evidence_ok is None else int(verdict.evidence_ok),
            verdict.confidence, verdict.stage.value, verdict.lane.value, verdict.model,
            verdict.template_id, json.dumps(verdict.reasons, ensure_ascii=False),
            int(verdict.needs_review), verdict.latency_ms, _dt_to_text(verdict.created_at),
        ),
    )


def row_to_verdict(row: sqlite3.Row) -> Verdict:
    return Verdict(
        message_id=row["message_id"], pack=row["pack"], category=row["category"],
        action_required=bool(row["action_required"]), urgency=row["urgency"],
        next_action=row["next_action"], summary=row["summary"], org=row["org"],
        org_domain=row["org_domain"], role=row["role"], contact_person=row["contact_person"],
        event_datetime=_text_to_dt(row["event_datetime"]), deadline=_text_to_date(row["deadline"]),
        amount=row["amount"], evidence=row["evidence"],
        evidence_ok=None if row["evidence_ok"] is None else bool(row["evidence_ok"]),
        confidence=row["confidence"], stage=row["stage"], lane=row["lane"], model=row["model"],
        template_id=row["template_id"], reasons=json.loads(row["reasons"] or "[]"),
        needs_review=bool(row["needs_review"]), latency_ms=row["latency_ms"],
        created_at=_text_to_dt(row["created_at"]) or datetime.now(timezone.utc),
    )


def get_verdict(conn: sqlite3.Connection, message_id: str, pack: str) -> Verdict | None:
    row = conn.execute(
        "SELECT * FROM verdicts WHERE message_id = ? AND pack = ?", (message_id, pack)
    ).fetchone()
    return row_to_verdict(row) if row else None


def iter_verdicts(
    conn: sqlite3.Connection,
    pack: str,
    *,
    since: datetime | None = None,
    action_required: bool | None = None,
    limit: int | None = None,
) -> list[tuple[Verdict, sqlite3.Row]]:
    sql = ["SELECT v.*, m.subject, m.from_addr, m.from_name, m.date_utc, m.thread_id",
           "FROM verdicts v JOIN messages m ON m.message_id = v.message_id",
           "WHERE v.pack = ?"]
    params: list[Any] = [pack]
    if since is not None:
        sql.append("AND m.date_utc >= ?")
        params.append(_dt_to_text(since))
    if action_required is not None:
        sql.append("AND v.action_required = ?")
        params.append(int(action_required))
    sql.append("ORDER BY m.date_utc DESC")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [(row_to_verdict(row), row) for row in rows]


def open_item_candidates(
    conn: sqlite3.Connection, pack: str
) -> list[tuple[Verdict, sqlite3.Row]]:
    """Every verdict that asked for something, with what decides if it still does.

    Each row carries the message's date and thread, and — when the message
    belongs to a case — that case's id and the date of its latest inbound mail,
    so the Brief can tell an open request from one the case has moved past.
    """
    rows = conn.execute(
        """
        SELECT v.*, m.subject, m.from_addr, m.from_name, m.date_utc, m.thread_id,
               k.case_id AS case_id, k.last_inbound_at AS case_last_inbound
        FROM verdicts v
        JOIN messages m ON m.message_id = v.message_id
        LEFT JOIN (
            SELECT cm.message_id, cm.case_id, c.last_inbound_at
            FROM case_messages cm JOIN cases c ON c.id = cm.case_id
            WHERE c.pack = ?
        ) k ON k.message_id = v.message_id
        WHERE v.pack = ? AND m.direction = 'inbound'
          AND (v.action_required = 1 OR v.urgency != 'none')
        ORDER BY m.date_utc DESC
        """,
        (pack, pack),
    ).fetchall()
    seen: set[str] = set()
    result: list[tuple[Verdict, sqlite3.Row]] = []
    for row in rows:
        if row["message_id"] in seen:  # a message in two cases: once is enough
            continue
        seen.add(row["message_id"])
        result.append((row_to_verdict(row), row))
    return result


def last_reply_by_thread(conn: sqlite3.Connection) -> dict[str, datetime]:
    """thread id -> when you last wrote in that thread."""
    rows = conn.execute(
        "SELECT thread_id, MAX(date_utc) AS at FROM messages "
        "WHERE direction = 'outbound' AND thread_id IS NOT NULL AND thread_id != '' "
        "GROUP BY thread_id"
    )
    replies: dict[str, datetime] = {}
    for row in rows:
        at = _text_to_dt(row["at"])
        if at is not None:
            replies[row["thread_id"]] = at
    return replies


def verdict_stats(conn: sqlite3.Connection, pack: str) -> dict[str, Any]:
    by_stage = {
        r["stage"]: r["c"]
        for r in conn.execute(
            "SELECT stage, COUNT(*) c FROM verdicts WHERE pack = ? GROUP BY stage", (pack,)
        )
    }
    by_category = {
        r["category"]: r["c"]
        for r in conn.execute(
            "SELECT category, COUNT(*) c FROM verdicts WHERE pack = ? GROUP BY category "
            "ORDER BY c DESC",
            (pack,),
        )
    }
    action = conn.execute(
        "SELECT COUNT(*) c FROM verdicts WHERE pack = ? AND action_required = 1", (pack,)
    ).fetchone()["c"]
    review = conn.execute(
        "SELECT COUNT(*) c FROM verdicts WHERE pack = ? AND needs_review = 1", (pack,)
    ).fetchone()["c"]
    return {
        "by_stage": by_stage,
        "by_category": by_category,
        "action_required": action,
        "needs_review": review,
        "total": sum(by_stage.values()),
    }


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #


def row_to_case(row: sqlite3.Row) -> Case:
    return Case(
        id=row["id"], pack=row["pack"], key=row["key"], title=row["title"], org=row["org"],
        org_domain=row["org_domain"], role=row["role"], state=row["state"],
        opened_at=_text_to_dt(row["opened_at"]), last_event_at=_text_to_dt(row["last_event_at"]),
        last_inbound_at=_text_to_dt(row["last_inbound_at"]),
        last_outbound_at=_text_to_dt(row["last_outbound_at"]),
        next_action=row["next_action"], next_action_due=_text_to_date(row["next_action_due"]),
        event_datetime=_text_to_dt(row["event_datetime"]), contact_person=row["contact_person"],
        message_count=row["message_count"], stalled=bool(row["stalled"]),
        closed=bool(row["closed"]), notes=row["notes"],
    )


def find_case(conn: sqlite3.Connection, pack: str, key: str) -> Case | None:
    row = conn.execute("SELECT * FROM cases WHERE pack = ? AND key = ?", (pack, key)).fetchone()
    return row_to_case(row) if row else None


def find_case_by_message(conn: sqlite3.Connection, pack: str, message_id: str) -> Case | None:
    row = conn.execute(
        "SELECT c.* FROM cases c JOIN case_messages cm ON cm.case_id = c.id "
        "WHERE cm.message_id = ? AND c.pack = ?",
        (message_id, pack),
    ).fetchone()
    return row_to_case(row) if row else None


def save_case(conn: sqlite3.Connection, case: Case) -> Case:
    """Insert or update one case.

    Deliberately a lookup followed by an INSERT or an UPDATE rather than an
    upsert: SQLite advances the AUTOINCREMENT sequence on a conflicting insert,
    which would leave visible gaps in the case ids the CLI shows people.
    """
    values = (
        case.title, case.org, case.org_domain, case.role, case.state,
        _dt_to_text(case.opened_at), _dt_to_text(case.last_event_at),
        _dt_to_text(case.last_inbound_at), _dt_to_text(case.last_outbound_at),
        case.next_action.value if hasattr(case.next_action, "value") else str(case.next_action),
        case.next_action_due.isoformat() if case.next_action_due else None,
        _dt_to_text(case.event_datetime), case.contact_person, case.message_count,
        int(case.stalled), int(case.closed), case.notes,
    )
    existing = conn.execute(
        "SELECT id, opened_at FROM cases WHERE pack = ? AND key = ?", (case.pack, case.key)
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO cases (title, org, org_domain, role, state, opened_at, last_event_at,
                last_inbound_at, last_outbound_at, next_action, next_action_due, event_datetime,
                contact_person, message_count, stalled, closed, notes, pack, key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*values, case.pack, case.key),
        )
        case.id = cursor.lastrowid
        return case

    opened = min(filter(None, [existing["opened_at"], _dt_to_text(case.opened_at)]), default=None)
    conn.execute(
        """
        UPDATE cases SET title = ?, org = ?, org_domain = ?, role = ?, state = ?,
            opened_at = ?, last_event_at = ?, last_inbound_at = ?, last_outbound_at = ?,
            next_action = ?, next_action_due = ?, event_datetime = ?, contact_person = ?,
            message_count = ?, stalled = ?, closed = ?, notes = ?
        WHERE id = ?
        """,
        (values[0], values[1], values[2], values[3], values[4], opened, *values[6:],
         existing["id"]),
    )
    case.id = existing["id"]
    return case


def link_message(conn: sqlite3.Connection, case_id: int, message_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO case_messages(case_id, message_id) VALUES (?, ?)",
        (case_id, message_id),
    )


def add_case_event(conn: sqlite3.Connection, event: CaseEvent) -> None:
    conn.execute(
        "INSERT INTO case_events(case_id, message_id, at, from_state, to_state, category, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            event.case_id, event.message_id, _dt_to_text(event.at), event.from_state,
            event.to_state, event.category, event.note,
        ),
    )


def list_cases(
    conn: sqlite3.Connection,
    pack: str,
    *,
    state: str | None = None,
    stalled: bool | None = None,
    open_only: bool = False,
) -> list[Case]:
    sql = ["SELECT * FROM cases WHERE pack = ?"]
    params: list[Any] = [pack]
    if state:
        sql.append("AND state = ?")
        params.append(state)
    if stalled is not None:
        sql.append("AND stalled = ?")
        params.append(int(stalled))
    if open_only:
        sql.append("AND closed = 0")
    sql.append("ORDER BY COALESCE(last_event_at, opened_at) DESC")
    return [row_to_case(r) for r in conn.execute(" ".join(sql), params)]


def case_timeline(conn: sqlite3.Connection, case_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.message_id, m.date_utc, m.subject, m.from_addr, m.direction,
               v.category, v.summary, v.evidence
        FROM case_messages cm
        JOIN messages m ON m.message_id = cm.message_id
        LEFT JOIN verdicts v ON v.message_id = m.message_id
        WHERE cm.case_id = ?
        ORDER BY m.date_utc ASC
        """,
        (case_id,),
    ).fetchall()


def case_message_ids(conn: sqlite3.Connection, case_id: int) -> list[str]:
    return [
        r["message_id"]
        for r in conn.execute(
            "SELECT message_id FROM case_messages WHERE case_id = ?", (case_id,)
        )
    ]


# --------------------------------------------------------------------------- #
# Labels, training, accounts
# --------------------------------------------------------------------------- #


def plan_label(conn: sqlite3.Connection, message_id: str, label: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO label_plan(message_id, label, created_at) VALUES (?,?,?)",
        (message_id, label, _dt_to_text(datetime.now(timezone.utc))),
    )


def pending_labels(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT lp.*, m.subject, m.from_addr FROM label_plan lp "
           "JOIN messages m ON m.message_id = lp.message_id WHERE lp.applied = 0 "
           "ORDER BY lp.created_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def mark_label_applied(conn: sqlite3.Connection, message_id: str, label: str) -> None:
    conn.execute(
        "UPDATE label_plan SET applied = 1, applied_at = ? WHERE message_id = ? AND label = ?",
        (_dt_to_text(datetime.now(timezone.utc)), message_id, label),
    )


def add_training_row(
    conn: sqlite3.Connection,
    message_id: str,
    pack: str,
    category: str,
    action_required: bool | None = None,
    source: str = "user",
) -> None:
    conn.execute(
        """
        INSERT INTO training(message_id, pack, category, action_required, source, created_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(message_id, pack, source) DO UPDATE SET
            category = excluded.category, action_required = excluded.action_required,
            created_at = excluded.created_at
        """,
        (
            message_id, pack, category,
            None if action_required is None else int(action_required),
            source, _dt_to_text(datetime.now(timezone.utc)),
        ),
    )


def training_rows(conn: sqlite3.Connection, pack: str) -> list[tuple[Message, str]]:
    rows = conn.execute(
        """
        SELECT m.*, b.body_text, b.body_raw_text, t.category
        FROM training t
        JOIN messages m ON m.message_id = t.message_id
        LEFT JOIN bodies b ON b.message_id = m.message_id
        WHERE t.pack = ?
        """,
        (pack,),
    ).fetchall()
    return [(row_to_message(r, r), r["category"]) for r in rows]


def upsert_account(
    conn: sqlite3.Connection, name: str, provider: str, address: str = ""
) -> None:
    conn.execute(
        "INSERT INTO accounts(name, provider, address, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET provider = excluded.provider, "
        "address = excluded.address",
        (name, provider, address, _dt_to_text(datetime.now(timezone.utc))),
    )


def list_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()


def get_cursor(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = conn.execute("SELECT cursor FROM accounts WHERE name = ?", (name,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["cursor"])
    except json.JSONDecodeError:
        return {}


def set_cursor(conn: sqlite3.Connection, name: str, cursor: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE accounts SET cursor = ?, last_sync = ? WHERE name = ?",
        (json.dumps(cursor), _dt_to_text(datetime.now(timezone.utc)), name),
    )


def own_addresses(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT address FROM accounts WHERE address != ''").fetchall()
    return {r["address"].lower() for r in rows}


def owner_name(conn: sqlite3.Connection) -> str:
    """The name you sign your own mail with, as your sent mail shows it."""
    row = conn.execute(
        "SELECT from_name, COUNT(*) AS n FROM messages "
        "WHERE direction = 'outbound' AND from_name != '' AND from_name NOT LIKE '%@%' "
        "GROUP BY from_name ORDER BY n DESC LIMIT 1"
    ).fetchone()
    return row["from_name"] if row else ""


def bulk_upsert(conn: sqlite3.Connection, messages: Iterable[Message]) -> tuple[int, int]:
    """Insert many messages in one transaction. Returns (new, seen)."""
    new = seen = 0
    with conn:
        for message in messages:
            seen += 1
            if upsert_message(conn, message):
                new += 1
    return new, seen


# --------------------------------------------------------------------------- #
# Browsing — the queries the UI needs, which the CLI never did
# --------------------------------------------------------------------------- #


def browse_messages(
    conn: sqlite3.Connection,
    pack: str,
    *,
    category: str | None = None,
    action_required: bool | None = None,
    needs_review: bool | None = None,
    lane: str | None = None,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """A filtered, paged message list with its triage verdict attached.

    Returns (rows, total matching) so the UI can show "showing 50 of 812"
    without a second round trip.
    """
    joins = [
        "FROM messages m",
        "LEFT JOIN verdicts v ON v.message_id = m.message_id AND v.pack = ?",
    ]
    params: list[Any] = [pack]
    where: list[str] = ["m.direction = 'inbound'"]

    if query.strip():
        # MATCH must name the FTS table itself. Not every SQLite build accepts a
        # table alias there, so the join stays unaliased.
        joins.append("JOIN messages_fts ON messages_fts.message_id = m.message_id")
        where.append("messages_fts MATCH ?")
        params.append(query.strip())
    if category:
        where.append("v.category = ?")
        params.append(category)
    if action_required is not None:
        where.append("COALESCE(v.action_required, 0) = ?")
        params.append(int(action_required))
    if needs_review is not None:
        where.append("COALESCE(v.needs_review, 0) = ?")
        params.append(int(needs_review))
    if lane:
        where.append("v.lane = ?")
        params.append(lane)

    clause = " ".join(joins) + " WHERE " + " AND ".join(where)
    select = (
        "SELECT m.message_id, m.subject, m.from_name, m.from_addr, m.date_utc, "
        "m.language, v.category, v.action_required, v.urgency, v.summary, v.confidence, "
        "v.stage, v.lane, v.needs_review, v.deadline, v.event_datetime, v.evidence, "
        "v.org, v.role "
    )

    def run(bound: list[Any]) -> tuple[list[dict[str, Any]], int]:
        total = conn.execute(f"SELECT COUNT(*) c {clause}", bound).fetchone()["c"]
        rows = conn.execute(
            f"{select}{clause} ORDER BY m.date_utc DESC LIMIT ? OFFSET ?",
            [*bound, int(limit), int(offset)],
        ).fetchall()
        return [dict(row) for row in rows], int(total)

    if not query.strip():
        # No user-supplied query means no syntax to get wrong: let errors surface.
        return run(params)
    try:
        return run(params)
    except sqlite3.OperationalError as exc:
        # Only a malformed *search expression* is recoverable. Anything else is a
        # bug in this query and must not be swallowed into an empty result.
        if not _is_fts_syntax_error(exc):
            raise
        phrase = '"' + query.strip().replace('"', '""') + '"'
        retried = [phrase if value == query.strip() else value for value in params]
        try:
            return run(retried)
        except sqlite3.OperationalError as inner:
            if not _is_fts_syntax_error(inner):
                raise
            return [], 0


_FTS_SYNTAX_HINTS = (
    "fts5", "unterminated", "syntax error", "no such column", "malformed match",
)


def _is_fts_syntax_error(exc: sqlite3.OperationalError) -> bool:
    """Distinguish a user's bad search string from a broken query of ours."""
    return any(hint in str(exc).lower() for hint in _FTS_SYNTAX_HINTS)


def message_detail(conn: sqlite3.Connection, message_id: str, pack: str) -> dict[str, Any] | None:
    """One message with its body, verdict and owning case — the detail pane."""
    row = conn.execute(
        "SELECT m.*, b.body_text, b.body_raw_text FROM messages m "
        "LEFT JOIN bodies b ON b.message_id = m.message_id WHERE m.message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    message = row_to_message(row, row)
    verdict = get_verdict(conn, message_id, pack)
    case = find_case_by_message(conn, pack, message_id)
    return {
        "message": {
            "message_id": message.message_id,
            "subject": message.subject,
            "from": {"name": message.from_.name, "address": message.from_.address},
            "to": [a.address for a in message.to],
            "date": _dt_to_text(message.date),
            "language": message.language,
            "folder": message.folder,
            "account": message.account,
            "provider": message.provider,
            "has_html": message.has_html,
            "attachments": [a.model_dump() for a in message.attachments],
            "body_text": message.body_text,
            # Everything the cleaning set aside (quoted replies, signature,
            # footer), for the reader who wants it; absent when nothing was.
            "body_full": message.body_raw_text
            if message.body_raw_text.strip() != message.body_text.strip() else None,
        },
        "verdict": verdict.model_dump(mode="json") if verdict else None,
        "case": case.model_dump(mode="json") if case else None,
    }


def case_labels_for_messages(
    conn: sqlite3.Connection, pack: str
) -> dict[str, tuple[str | None, str | None]]:
    """message_id -> (organisation, role) of the case it belongs to.

    A case carries the tidied-up name — "Weser Data GmbH" — while a single
    message may only have what could be scraped from a sender domain
    ("Weserdata"). When both exist the case wins.
    """
    rows = conn.execute(
        "SELECT cm.message_id, c.org, c.role FROM case_messages cm "
        "JOIN cases c ON c.id = cm.case_id WHERE c.pack = ?",
        (pack,),
    )
    return {row["message_id"]: (row["org"], row["role"]) for row in rows}


def states_reached(conn: sqlite3.Connection, pack: str) -> dict[int, list[str]]:
    """Every state each case has actually been in, from its recorded events.

    Drawing a progress rail from the current state alone would imply the case
    passed through every earlier stage, which is usually false: most
    applications go straight from acknowledged to rejected without an interview.
    """
    reached: dict[int, list[str]] = {}
    rows = conn.execute(
        "SELECT e.case_id, e.to_state FROM case_events e "
        "JOIN cases c ON c.id = e.case_id "
        "WHERE c.pack = ? AND e.to_state IS NOT NULL ORDER BY e.id",
        (pack,),
    )
    for row in rows:
        seen = reached.setdefault(int(row["case_id"]), [])
        if row["to_state"] not in seen:
            seen.append(row["to_state"])
    return reached


def category_counts(conn: sqlite3.Connection, pack: str) -> list[dict[str, Any]]:
    return [
        {"category": row["category"], "count": row["c"]}
        for row in conn.execute(
            "SELECT category, COUNT(*) c FROM verdicts WHERE pack = ? GROUP BY category "
            "ORDER BY c DESC",
            (pack,),
        )
    ]


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)
        )
    ]


def correct_verdict(
    conn: sqlite3.Connection,
    message_id: str,
    pack: str,
    category: str,
    action_required: bool | None = None,
) -> bool:
    """Record a human correction: update the verdict and store a training row."""
    existing = conn.execute(
        "SELECT 1 FROM verdicts WHERE message_id = ? AND pack = ?", (message_id, pack)
    ).fetchone()
    if existing is None:
        return False
    if action_required is None:
        conn.execute(
            "UPDATE verdicts SET category = ?, needs_review = 0, stage = 'manual' "
            "WHERE message_id = ? AND pack = ?",
            (category, message_id, pack),
        )
    else:
        conn.execute(
            "UPDATE verdicts SET category = ?, action_required = ?, needs_review = 0, "
            "stage = 'manual' WHERE message_id = ? AND pack = ?",
            (category, int(action_required), message_id, pack),
        )
    add_training_row(conn, message_id, pack, category, action_required, source="user")
    conn.commit()
    return True
