"""The contact-graph feature.

"Have I ever written to this address, or to anyone at this domain?" is the
single best importance signal available, and it is one indexed lookup. It is
also the feature a cloud product cannot copy cheaply, because it needs your
whole sent-mail history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from bmail.models import Message
from bmail.store import repo


@dataclass(slots=True)
class ContactSignals:
    known_address: bool = False
    known_domain: bool = False
    sent_to_address: int = 0
    sent_to_domain: int = 0
    received_from_address: int = 0

    @property
    def familiarity(self) -> float:
        """0.0 = never heard of them, 1.0 = an ongoing correspondence."""
        if self.sent_to_address:
            return 1.0
        if self.sent_to_domain:
            return 0.7
        if self.received_from_address > 3:
            return 0.4
        if self.received_from_address:
            return 0.2
        return 0.0


def contact_signals(conn: sqlite3.Connection, message: Message) -> ContactSignals:
    stats = repo.contact_stats(conn, message.from_.address, message.sender_domain)
    return ContactSignals(
        known_address=stats["sent_to_address"] > 0,
        known_domain=stats["sent_to_domain"] > 0,
        sent_to_address=stats["sent_to_address"],
        sent_to_domain=stats["sent_to_domain"],
        received_from_address=stats["received_from_address"],
    )
