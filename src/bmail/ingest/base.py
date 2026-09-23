"""The one interface every provider hides behind.

Providers churn. Gmail changes its scopes, Microsoft retires basic auth, an IMAP
host renumbers UIDs. Assume each connector gets rewritten once and keep the
surface this small so the rewrite stays local.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from bmail.models import Message


@dataclass(slots=True)
class SyncResult:
    fetched: int = 0   # messages the source handed over
    new: int = 0       # of those, the ones the store had not seen before
    cursor: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class SourceConnector(Protocol):
    """Pull messages from one account."""

    name: str
    provider: str

    def fetch(
        self, cursor: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> Iterator[Message]:
        """Yield canonical messages, newest-first or oldest-first — either is fine."""

    def cursor(self) -> dict[str, Any]:
        """Return the resume token to persist after a successful fetch."""
