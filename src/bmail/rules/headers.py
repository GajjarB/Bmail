"""Header-level bulk detection.

RFC-defined headers tell you a message is machine-sent far more reliably than
any classifier, and reading them costs nothing. This is the cheapest 40% of the
whole pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bmail.models import Message

_NOREPLY = re.compile(
    r"(^|[._+-])(no[._-]?reply|do[._-]?not[._-]?reply|donotreply|noreply|nicht[._-]?antworten|"
    r"ne[._-]?pas[._-]?repondre|mailer[._-]?daemon|postmaster|notifications?|automated|"
    r"auto[._-]?reply|bounce)",
    re.IGNORECASE,
)

# Mailboxes a system sends from: banks, shops, carriers, account services.
# Not the ones a recruiter writes from (jobs@, karriere@, hr@, talent@), which
# stay person-like on purpose: a person there may be asking you something.
_SYSTEM_MAILBOX = re.compile(
    r"^(alerts?|support|service|services|kundenservice|customer[._-]?service|billing|"
    r"invoices?|rechnung|orders?|bestellung|auftrag\w*|versand|shipping|delivery|tracking|"
    r"booking|buchung|reservations?|tickets?|accounts?|account[._-]?\w+|security|"
    r"enq[._-]?\w*|statements?|transactions?|payments?|shop|store|dsl[._-]?\w*)$",
    re.IGNORECASE,
)

_BULK_MAILERS = (
    "mailchimp", "sendgrid", "mandrill", "amazonses", "sparkpost", "postmark",
    "klaviyo", "hubspot", "marketo", "salesforce", "braze", "customer.io",
    "cleverreach", "rapidmail", "newsletter2go", "brevo", "sendinblue",
)

_BULK_ADDRESS_HINTS = (
    "newsletter", "marketing", "info@", "news@", "updates@", "digest@",
    "notification", "mailing", "campaign",
)


@dataclass(slots=True)
class HeaderSignals:
    is_bulk: bool = False
    is_automated: bool = False
    is_noreply: bool = False
    is_list: bool = False
    is_system: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """0.0 = clearly a person, 1.0 = clearly a machine blast."""
        weights = {
            "is_list": 0.45,
            "is_bulk": 0.35,
            "is_noreply": 0.25,
            "is_system": 0.25,
            "is_automated": 0.30,
        }
        total = sum(value for name, value in weights.items() if getattr(self, name))
        return min(1.0, total)


def header_signals(message: Message) -> HeaderSignals:
    signals = HeaderSignals()
    headers = {name.lower(): value for name, value in message.headers.items()}

    if message.list_id or message.list_unsubscribe or headers.get("list-unsubscribe"):
        signals.is_list = True
        signals.is_bulk = True
        signals.reasons.append("List-Unsubscribe/List-Id header present")

    precedence = (message.precedence or "").lower()
    if precedence in ("bulk", "list", "junk"):
        signals.is_bulk = True
        signals.reasons.append(f"Precedence: {precedence}")

    auto = (message.auto_submitted or "").lower()
    if auto and auto != "no":
        signals.is_automated = True
        signals.reasons.append(f"Auto-Submitted: {auto}")

    if headers.get("x-auto-response-suppress"):
        signals.is_automated = True
        signals.reasons.append("X-Auto-Response-Suppress header present")

    if headers.get("feedback-id") or headers.get("x-campaign-id"):
        signals.is_bulk = True
        signals.reasons.append("campaign tracking header present")

    mailer = (message.x_mailer or "").lower()
    for vendor in _BULK_MAILERS:
        if vendor in mailer or vendor in (message.return_path or "").lower():
            signals.is_bulk = True
            signals.reasons.append(f"bulk sending platform: {vendor}")
            break

    address = message.from_.address.lower()
    local = address.split("@")[0] if "@" in address else address
    if _NOREPLY.search(local):
        signals.is_noreply = True
        signals.reasons.append("no-reply sender address")
    elif _SYSTEM_MAILBOX.match(local):
        signals.is_system = True
        signals.reasons.append("system mailbox sender")
    elif any(hint in address for hint in _BULK_ADDRESS_HINTS):
        signals.is_bulk = True
        signals.reasons.append("bulk-style sender address")

    return signals
