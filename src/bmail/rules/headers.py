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
    r"^(alerts?|support|(?![\w.-]*(?:bewerb|recruit|karriere|career|personal|job|talent)|hr)"
    r"[\w.-]*service|services|billing|"
    r"invoices?|rechnung\w*|orders?|bestellung|auftrag\w*|versand|shipping|delivery|tracking|"
    r"booking|buchung|reservations?|tickets?|accounts?|account[._-]?\w+|security|"
    r"enq[._-]?\w*|statements?|transactions?|payments?|shop|store|dsl[._-]?\w*|"
    r"system|robot|mailrobot|communications?|\w*auto[._-]?e?-?mail\w*)$",
    re.IGNORECASE,
)

_BULK_MAILERS = (
    "mailchimp", "sendgrid", "mandrill", "amazonses", "sparkpost", "postmark",
    "klaviyo", "hubspot", "marketo", "salesforce", "braze", "customer.io",
    "cleverreach", "rapidmail", "newsletter2go", "brevo", "sendinblue",
)

# The envelope sender of mail a sending service delivers: a bounce mailbox with
# the recipient written into it ("bounces+1234-ab=you=gmail.com@em.shop.com"),
# or one long serial number. A person's mail comes back to the person.
_BOUNCE_MAILBOX = re.compile(r"^(?:bounces?|postmaster|mailer-daemon)(?:$|[+._=-])", re.IGNORECASE)
_RECIPIENT_IN_ADDRESS = re.compile(r"=[\w.-]+\.[a-z]{2,}$", re.IGNORECASE)
# Forwarders and spam filters rewrite a person's own address this way.
_REWRITTEN_SENDER = re.compile(r"^(?:srs\d?|prvs|btv\d?)=", re.IGNORECASE)
_SERVICE_HEADERS = ("x-sg-eid", "x-ses-outgoing", "x-mailgun-tag")
_REPLY_SUBJECT = re.compile(r"^\s*(?:re|aw|antw|sv|rif)\s*(?:\[\d+\])?\s*:", re.IGNORECASE)

# Of the platforms above, the ones that carry anything at all: a password
# reset, an invoice, an interview invitation. The rest only send campaigns.
_DELIVERY_SERVICES = ("sendgrid", "mandrill", "amazonses", "sparkpost", "postmark")

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
    # Delivered by a sending service, not from someone's own mailbox. Not part
    # of the score: an applicant-tracking system delivers a recruiter's own
    # words this way, and that must never make them bulk.
    is_relayed: bool = False
    # Sent with a tool that exists to run campaigns.
    is_campaign: bool = False
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

    @property
    def machine(self) -> bool:
        """Whether anything at all says software sent this, not a person."""
        return self.score > 0 or self.is_relayed


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
            signals.is_campaign = vendor not in _DELIVERY_SERVICES
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

    # A reply in a conversation is a person's, however it was carried: hosts,
    # sellers and support staff answer through their platform's mail service.
    if not (message.in_reply_to or _REPLY_SUBJECT.match(message.subject)):
        if _sent_by_a_service(message.return_path or "", headers):
            signals.is_relayed = True
            signals.reasons.append("delivered by a sending service")

    return signals


def _sent_by_a_service(return_path: str, headers: dict[str, str]) -> bool:
    if any(headers.get(name) for name in _SERVICE_HEADERS):
        return True
    local = return_path.strip().strip("<>").rsplit("@", 1)[0]
    if not local or _REWRITTEN_SENDER.match(local):
        return False
    if _BOUNCE_MAILBOX.match(local) or _RECIPIENT_IN_ADDRESS.search(local):
        return True
    return len(local) >= 30 and sum(ch.isdigit() for ch in local) >= 10
