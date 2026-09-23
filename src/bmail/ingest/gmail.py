"""Gmail source, incremental via `historyId`.

Scope policy, and it is deliberate:

* `gmail.readonly` is the default and is all Bmail needs to triage.
* `gmail.modify` is requested only when you turn label write-back on.
* `https://mail.google.com/` (full access, including permanent delete) is never
  requested. It buys nothing here and pushes the app into a stricter
  verification tier.

Because no mail content ever leaves the machine, this stays a client-side
integration. Each user supplies their own OAuth client from their own Google
Cloud project — see docs/PROVIDERS.md — so there is no shared server holding
anyone's tokens.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bmail.errors import IngestError, SignInRequired
from bmail.ingest.raw import parse_message
from bmail.models import Message

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def _require_libs() -> tuple[Any, Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise IngestError(
            "Gmail support needs the optional extra: pip install 'bmail[gmail]'"
        ) from exc
    return Request, Credentials, InstalledAppFlow, build


class GmailSource:
    provider = "gmail"

    def __init__(
        self,
        *,
        name: str = "gmail",
        client_secret_file: str | Path,
        token_file: str | Path,
        allow_modify: bool = False,
        query: str = "",
        own_addresses: set[str] | None = None,
        interactive: bool = True,
    ) -> None:
        self.name = name
        # False when nobody is at the keyboard — an automatic sync. A token that
        # can be refreshed silently still is; anything that needs a browser
        # raises SignInRequired instead of opening one.
        self.interactive = interactive
        self.client_secret_file = Path(client_secret_file).expanduser()
        self.token_file = Path(token_file).expanduser()
        self.scopes = [MODIFY_SCOPE] if allow_modify else [READONLY_SCOPE]
        self.query = query
        self.own_addresses = own_addresses or set()
        self._cursor: dict[str, Any] = {}
        self._service: Any = None

    # -- auth ---------------------------------------------------------------- #

    def service(self) -> Any:
        if self._service is not None:
            return self._service
        Request, Credentials, InstalledAppFlow, build = _require_libs()
        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), self.scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif not self.interactive:
                raise SignInRequired(
                    f"Gmail account '{self.name}' needs you to sign in again — "
                    "use Check for new mail, or run `bmail ingest`, to do it"
                )
            else:
                if not self.client_secret_file.exists():
                    raise IngestError(
                        f"missing OAuth client file {self.client_secret_file}. Create a Desktop "
                        "app client in your own Google Cloud project and download it."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_secret_file), self.scopes
                )
                creds = flow.run_local_server(port=0)
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(creds.to_json(), encoding="utf-8")
            self.token_file.chmod(0o600)
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def address(self) -> str:
        profile = self.service().users().getProfile(userId="me").execute()
        return str(profile.get("emailAddress", ""))

    # -- fetch ---------------------------------------------------------------- #

    def fetch(
        self, cursor: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> Iterator[Message]:
        """First run: list the mailbox. After that: follow history.

        The cursor that comes out is the part that matters. Behind where it
        should be only costs a re-read; ahead of it skips mail for good.
        """
        service = self.service()
        cursor = dict(cursor or {})
        history_id = cursor.get("history_id")
        self._cursor = {}

        profile: dict[str, Any] = {}
        if not history_id or not self.own_addresses:
            profile = service.users().getProfile(userId="me").execute()
        own = self.own_addresses or {str(profile.get("emailAddress", "")).lower()}

        if history_id:
            listing = _History(service, str(history_id))
            ids: Iterator[str] = iter(listing)
        else:
            # The mailbox's position *before* the listing starts. A message's own
            # historyId moves every time it changes, so the highest one among
            # the messages fetched can sit past mail that arrived mid-listing —
            # and a cursor there skips that mail for ever.
            listing = None
            ids = self._ids_full(service, limit)

        seen: set[str] = set()
        count = 0
        truncated = False
        for message_id in ids:
            if message_id in seen:       # history can list one message twice
                continue
            seen.add(message_id)
            if limit is not None and count >= limit:
                truncated = True
                break
            try:
                payload = (
                    service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="raw")
                    .execute()
                )
            except Exception as exc:
                if _status(exc) == 404:
                    continue  # deleted between being listed and being fetched
                raise IngestError(f"Gmail could not return message {message_id}: {exc}") from exc
            raw = base64.urlsafe_b64decode(payload["raw"].encode("ascii"))
            yield parse_message(
                raw,
                provider=self.provider,
                account=self.name,
                uid=message_id,
                thread_id=payload.get("threadId"),
                own_addresses=own,
            )
            count += 1

        if listing is not None:
            # A run stopped part-way through history keeps the old cursor: the
            # unread remainder is still ahead of it. Saving a later one would
            # skip it. A complete run moves to where the history listing ended.
            if not truncated and listing.latest:
                self._cursor = {"history_id": listing.latest}
        elif profile.get("historyId"):
            self._cursor = {"history_id": str(profile["historyId"])}

    def _ids_full(self, service: Any, limit: int | None) -> Iterator[str]:
        page: str | None = None
        seen = 0
        while True:
            response = (
                service.users()
                .messages()
                .list(userId="me", q=self.query or None, pageToken=page, maxResults=500)
                .execute()
            )
            for item in response.get("messages", []):
                yield item["id"]
                seen += 1
                if limit is not None and seen >= limit:
                    return
            page = response.get("nextPageToken")
            if not page:
                return

    def cursor(self) -> dict[str, Any]:
        return self._cursor

    # -- write-back ------------------------------------------------------------ #

    def apply_label(self, message_id: str, label: str) -> None:
        """Add one label. Requires `gmail.modify`; never deletes anything."""
        if MODIFY_SCOPE not in self.scopes:
            raise IngestError("label write-back needs allow_modify = true on the account")
        service = self.service()
        existing = {
            item["name"]: item["id"]
            for item in service.users().labels().list(userId="me").execute().get("labels", [])
        }
        label_id = existing.get(label)
        if label_id is None:
            created = (
                service.users()
                .labels()
                .create(userId="me", body={"name": label})
                .execute()
            )
            label_id = created["id"]
        service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()


class _History:
    """Message ids added since a history id, and where the listing ended.

    Drafts are skipped: they appear in history as messages added, and a
    half-written reply is not mail anyone sent you.
    """

    def __init__(self, service: Any, start: str) -> None:
        self.service = service
        self.start = start
        self.latest = ""

    def __iter__(self) -> Iterator[str]:
        page: str | None = None
        while True:
            try:
                response = (
                    self.service.users()
                    .history()
                    .list(userId="me", startHistoryId=self.start, pageToken=page)
                    .execute()
                )
            except Exception as exc:
                if _status(exc) == 404:  # Gmail keeps history for about a week
                    raise IngestError(
                        "Gmail history cursor expired; run a full re-sync with --reset"
                    ) from exc
                # Anything else is not an expired cursor, and saying it was
                # would tell someone to throw away their sync state over a
                # passing server error.
                raise IngestError(f"Gmail history request failed: {exc}") from exc
            self.latest = str(response.get("historyId") or self.latest)
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    message = added.get("message", {})
                    if "DRAFT" in (message.get("labelIds") or []):
                        continue
                    yield message["id"]
            page = response.get("nextPageToken")
            if not page:
                return


def _status(exc: BaseException) -> int | None:
    """The HTTP status of a googleapiclient error, without importing it."""
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None) or getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None
