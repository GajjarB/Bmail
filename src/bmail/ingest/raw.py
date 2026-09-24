"""RFC 5322 bytes -> canonical `Message`.

Every connector funnels through `parse_message`, so provider quirks cannot leak
past this file. Two decisions matter here:

* threading uses `References`/`In-Reply-To`, never subject matching — it is the
  only thing that works consistently across providers;
* attachment payloads are counted, never stored.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from bmail.models import Attachment, Direction, EmailAddress, Message
from bmail.text.clean import clean_body, detect_language, normalise_whitespace
from bmail.text.html import html_to_text, looks_like_html

KEEP_HEADERS = (
    "list-id", "list-unsubscribe", "precedence", "auto-submitted", "x-mailer",
    "x-auto-response-suppress", "authentication-results", "return-path",
    "x-campaign-id", "x-mailgun-tag", "feedback-id", "x-google-appliance",
    "x-priority", "importance", "x-ses-outgoing", "x-sg-eid", "x-report-abuse",
)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return normalise_whitespace(str(make_header(decode_header(value))))
    except Exception:
        return normalise_whitespace(value)


def _addresses(msg: EmailMessage, field: str) -> list[EmailAddress]:
    raw = msg.get_all(field, [])
    out: list[EmailAddress] = []
    for name, address in getaddresses([str(v) for v in raw]):
        if not address:
            continue
        out.append(EmailAddress(name=_decode(name), address=address.strip().lower()))
    return out


def _parse_date(msg: EmailMessage) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(str(raw))
            if parsed is not None:
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _references(msg: EmailMessage) -> list[str]:
    raw = " ".join(str(v) for v in msg.get_all("References", []))
    return [ref for ref in raw.replace(",", " ").split() if ref.startswith("<")]


def _bodies(msg: EmailMessage) -> tuple[str, bool, list[Attachment]]:
    """Return (plain text, had_html, attachments)."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = _decode(part.get_filename())
        if disposition == "attachment" or (filename and content_type not in ("text/plain",)):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                Attachment(
                    filename=filename or "unnamed",
                    content_type=content_type,
                    size=len(payload),
                )
            )
            continue
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except (AttributeError, ValueError):
            continue
        if payload is None:
            continue
        text = decode_payload(payload, part.get_content_charset())
        # Some senders label a whole HTML document text/plain.
        if content_type == "text/html" or looks_like_html(text):
            html_parts.append(text)
        else:
            plain_parts.append(text)

    plain = "\n\n".join(plain_parts)
    if not html_parts:
        return plain, False, attachments
    html = html_to_text("\n".join(html_parts))
    return (html if _html_says_more(plain, html) else plain), True, attachments


def decode_payload(payload: bytes, charset: str | None) -> str:
    """Bytes to text, whatever the part claims its charset is.

    A charset Python has never heard of ("x-unknown", "unknown-8bit") must not
    cost the message its body: try UTF-8, then Windows-1252, which is what
    mislabelled European mail almost always is.
    """
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            pass
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("cp1252", errors="replace")


# What a plain-text part says when its sender only really wrote the HTML one.
_STUB = re.compile(
    r"html[- ]?(capable|compatible|version|format|e-?mail)|(view|read|open|see) (this|the) "
    r"(e-?mail|message|newsletter) in (a|your) (web )?browser|does not support html|"
    r"im browser (an)?zeigen|im browser (ansehen|lesen|öffnen)|webversion|html-darstellung",
    re.I,
)


def _html_says_more(plain: str, html: str) -> bool:
    """Whether to read the HTML part instead of the plain-text one.

    The plain part wins when it is a real rendering of the mail: it is what
    the sender chose to write as text. It loses when it is missing, when it is
    a stub pointing at the HTML ("view this email in your browser"), or when it
    is a fraction of what the HTML says.
    """
    plain, html = plain.strip(), html.strip()
    if not plain:
        return True
    if not html:
        return False
    if len(plain) < 600 and _STUB.search(plain) and len(html) > len(plain):
        return True
    return len(plain) * 3 < len(html) and len(html) - len(plain) > 300


def synth_message_id(from_addr: str, date: datetime, subject: str, body: str) -> str:
    """Deterministic stand-in for a missing Message-ID, so re-ingest stays idempotent."""
    digest = hashlib.blake2b(
        "\x1f".join([from_addr, date.isoformat(), subject, body[:2000]]).encode(),
        digest_size=16,
    ).hexdigest()
    return f"<bmail-{digest}@local>"


def parse_message(
    raw: bytes | str,
    *,
    provider: str = "file",
    account: str = "default",
    uid: str | None = None,
    folder: str | None = None,
    thread_id: str | None = None,
    own_addresses: set[str] | None = None,
) -> Message:
    """Parse one RFC 5322 message into the canonical record."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]

    body_raw, has_html, attachments = _bodies(msg)
    body_raw = normalise_whitespace(body_raw)
    body_clean = clean_body(body_raw)
    subject = _decode(msg.get("Subject"))
    date = _parse_date(msg)

    senders = _addresses(msg, "From")
    sender = senders[0] if senders else EmailAddress()
    to = _addresses(msg, "To")
    cc = _addresses(msg, "Cc")
    reply_to = _addresses(msg, "Reply-To")

    message_id = _decode(msg.get("Message-ID")).strip()
    if not message_id.startswith("<"):
        message_id = synth_message_id(sender.address, date, subject, body_clean)

    own = {a.lower() for a in (own_addresses or set())}
    direction = Direction.outbound if own and sender.address.lower() in own else Direction.inbound

    references = _references(msg)
    in_reply_to = _decode(msg.get("In-Reply-To")).strip() or None
    if thread_id is None:
        # Root of the References chain is the most portable thread identifier.
        thread_id = references[0] if references else (in_reply_to or message_id)

    headers = {
        name: _decode(msg.get(name))
        for name in KEEP_HEADERS
        if msg.get(name) is not None
    }

    return Message(
        message_id=message_id,
        provider=provider,
        account=account,
        uid=uid,
        folder=folder,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        **{"from": sender},
        to=to,
        cc=cc,
        reply_to=reply_to[0] if reply_to else None,
        date=date,
        subject=subject,
        list_id=headers.get("list-id"),
        list_unsubscribe=headers.get("list-unsubscribe"),
        precedence=headers.get("precedence"),
        auto_submitted=headers.get("auto-submitted"),
        x_mailer=headers.get("x-mailer"),
        auth_results=headers.get("authentication-results"),
        return_path=headers.get("return-path"),
        has_html=has_html,
        language=detect_language(f"{subject}\n{body_clean}"),
        attachments=attachments,
        direction=direction,
        headers=headers,
        body_text=body_clean,
        body_raw_text=body_raw,
    )
