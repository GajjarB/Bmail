"""Provider label write-back — planned first, applied only on request.

The one thing Sift ever writes back to a mail provider is a label. It never
deletes, never moves, never sends. Even that is two steps: `plan` records what
would be applied, and `apply` performs it only when explicitly asked and only
for an account configured with `allow_modify = true`.

The worst case for a triage mistake should be a wrong row in a local database,
not a mangled mailbox.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from sift.errors import ExportError
from sift.packs import Pack
from sift.store import repo


@dataclass(slots=True)
class LabelPlan:
    planned: int = 0
    already: int = 0
    labels: dict[str, int] | None = None


def plan_labels(conn: sqlite3.Connection, pack: Pack, *, limit: int | None = None) -> LabelPlan:
    """Record the label each triaged message would get. Writes nothing outward."""
    counts: dict[str, int] = {}
    planned = already = 0
    for verdict, _row in repo.iter_verdicts(conn, pack.name, limit=limit):
        label = pack.label_for(verdict.category)
        existing = conn.execute(
            "SELECT applied FROM label_plan WHERE message_id = ? AND label = ?",
            (verdict.message_id, label),
        ).fetchone()
        if existing is not None:
            already += 1
            continue
        repo.plan_label(conn, verdict.message_id, label)
        counts[label] = counts.get(label, 0) + 1
        planned += 1
    conn.commit()
    return LabelPlan(planned=planned, already=already, labels=counts)


def apply_labels(
    conn: sqlite3.Connection, source, *, limit: int | None = None, dry_run: bool = True
) -> dict[str, int]:
    """Apply the pending plan through a connector that supports write-back."""
    pending = repo.pending_labels(conn, limit)
    if dry_run:
        return {"would_apply": len(pending), "applied": 0, "failed": 0}
    if not hasattr(source, "apply_label"):
        raise ExportError(
            f"provider '{getattr(source, 'provider', '?')}' cannot write labels back"
        )
    applied = failed = 0
    for row in pending:
        message = repo.get_message(conn, row["message_id"])
        if message is None or not message.uid:
            failed += 1
            continue
        try:
            source.apply_label(message.uid, row["label"])
            repo.mark_label_applied(conn, row["message_id"], row["label"])
            applied += 1
        except Exception:
            failed += 1
    conn.commit()
    return {"would_apply": len(pending), "applied": applied, "failed": failed}
