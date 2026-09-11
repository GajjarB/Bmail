"""Canonical records shared by every stage of the pipeline.

One rule holds throughout: a `Message` is the only representation of mail that
crosses module boundaries. Provider-specific shapes die inside their connector.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Direction(str, Enum):
    inbound = "inbound"
    outbound = "outbound"


class Lane(str, Enum):
    """Where Stage 1 sent a message."""

    bulk = "bulk"          # newsletters, notifications, no-reply blasts
    template = "template"  # matched a known ATS/portal recipe, extracted verbatim
    duplicate = "duplicate"
    candidate = "candidate"  # needs Stage 2/3


class Stage(str, Enum):
    rules = "rules"
    classifier = "classifier"
    llm = "llm"
    manual = "manual"


class Urgency(str, Enum):
    today = "today"
    this_week = "this_week"
    whenever = "whenever"
    none = "none"


class NextAction(str, Enum):
    none = "none"
    reply = "reply"
    schedule = "schedule"
    submit_document = "submit_document"
    prepare = "prepare"


# Categories are declared by packs, but these are the shared baseline every
# pack inherits, so the router and the UI always have something to key on.
BASE_CATEGORIES: tuple[str, ...] = (
    "acknowledgement",
    "rejection",
    "interview_invite",
    "offer",
    "assessment",
    "document_request",
    "scheduling",
    "newsletter",
    "admin",
    "personal",
    "other",
)


# --------------------------------------------------------------------------- #
# Mail
# --------------------------------------------------------------------------- #


class EmailAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    address: str = ""

    @property
    def domain(self) -> str:
        _, _, dom = self.address.partition("@")
        return dom.lower()

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name} <{self.address}>".strip() if self.name else self.address


class Attachment(BaseModel):
    filename: str = ""
    content_type: str = ""
    size: int = 0
    # Attachment payloads are deliberately not stored. Bmail indexes the fact
    # that a document was attached, never the document itself.


class Message(BaseModel):
    """The canonical message. Everything downstream reads only this."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    message_id: str
    provider: str = "file"
    account: str = "default"
    uid: str | None = None
    folder: str | None = None

    thread_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)

    from_: EmailAddress = Field(default_factory=EmailAddress, alias="from")
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    reply_to: EmailAddress | None = None

    date: datetime
    subject: str = ""

    list_id: str | None = None
    list_unsubscribe: str | None = None
    precedence: str | None = None
    auto_submitted: str | None = None
    x_mailer: str | None = None
    auth_results: str | None = None
    return_path: str | None = None

    body_text: str = ""       # cleaned: html stripped, quotes and signature removed
    body_raw_text: str = ""   # full plain text before quote removal, for evidence checks
    has_html: bool = False
    language: str = "und"
    attachments: list[Attachment] = Field(default_factory=list)

    direction: Direction = Direction.inbound
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("date")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)

    @property
    def sender_domain(self) -> str:
        return self.from_.domain

    @property
    def recipients(self) -> list[EmailAddress]:
        return [*self.to, *self.cc]

    def evidence_haystack(self) -> str:
        """Text an evidence span is allowed to have come from."""
        return "\n".join([self.subject, self.body_raw_text or self.body_text])


# --------------------------------------------------------------------------- #
# Triage output
# --------------------------------------------------------------------------- #


class Verdict(BaseModel):
    """What the pipeline concluded about one message."""

    message_id: str
    pack: str
    category: str = "other"
    action_required: bool = False
    urgency: Urgency = Urgency.none
    next_action: NextAction = NextAction.none
    summary: str = ""

    org: str | None = None
    org_domain: str | None = None
    role: str | None = None
    contact_person: str | None = None
    event_datetime: datetime | None = None
    deadline: date | None = None
    amount: str | None = None

    evidence: str | None = None
    evidence_ok: bool | None = None  # None = nothing to check
    confidence: float = 0.0
    stage: Stage = Stage.rules
    lane: Lane = Lane.candidate
    model: str | None = None
    template_id: str | None = None
    reasons: list[str] = Field(default_factory=list)
    needs_review: bool = False
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def merge_defaults(self, other: Verdict) -> Verdict:
        """Fill empty fields of this verdict from `other` (deterministic wins)."""
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if data.get(key) in (None, "", [], 0.0) and value not in (None, "", []):
                data[key] = value
        return Verdict(**data)


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #


class Case(BaseModel):
    """An entity with state — an application, a candidate, an invoice thread."""

    id: int | None = None
    pack: str
    key: str
    title: str = ""
    org: str | None = None
    org_domain: str | None = None
    role: str | None = None
    state: str = ""
    opened_at: datetime | None = None
    last_event_at: datetime | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    next_action: NextAction = NextAction.none
    next_action_due: date | None = None
    event_datetime: datetime | None = None
    contact_person: str | None = None
    message_count: int = 0
    stalled: bool = False
    closed: bool = False
    notes: str = ""

    def days_silent(self, now: datetime | None = None) -> int | None:
        ref = self.last_inbound_at or self.last_event_at
        if ref is None:
            return None
        now = now or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return max(0, (now - ref).days)


class CaseEvent(BaseModel):
    case_id: int
    message_id: str | None = None
    at: datetime
    from_state: str | None = None
    to_state: str | None = None
    category: str | None = None
    note: str = ""


class BriefItem(BaseModel):
    """One line of the daily Brief.

    The fields are structured rather than pre-formatted: a display layer that
    has to split a string back apart is a layer that will get it wrong.
    """

    kind: str  # needs_you | deadline | stalled | event | archived
    title: str
    detail: str = ""
    org: str | None = None
    role: str | None = None
    category: str = ""
    urgency: Urgency = Urgency.none
    when: datetime | None = None
    due: date | None = None
    message_id: str | None = None
    case_id: int | None = None
    score: float = 0.0
    evidence: str | None = None


class Brief(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    window_days: int = 1
    pack: str = ""
    needs_you: list[BriefItem] = Field(default_factory=list)
    deadlines: list[BriefItem] = Field(default_factory=list)
    events: list[BriefItem] = Field(default_factory=list)
    stalled: list[BriefItem] = Field(default_factory=list)
    archived: list[BriefItem] = Field(default_factory=list)
    counts: dict[str, Any] = Field(default_factory=dict)
