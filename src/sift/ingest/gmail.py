"""Gmail source, incremental via `historyId`.

Scope policy, and it is deliberate:

* `gmail.readonly` is the default and is all Sift needs to triage.
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

from sift.errors import IngestError
from sift.ingest.raw import parse_message
from sift.models import Message

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
            "Gmail support needs the optional extra: pip install 'sift-mail[gmail]'"
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
    ) -> None:
        self.name = name
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
        service = self.service()
        cursor = dict(cursor or {})
        history_id = cursor.get("history_id")
        ids = (
            self._ids_since(service, history_id)
            if history_id
            else self._ids_full(service, limit)
        )
        own = self.own_addresses or {self.address().lower()}

        newest_history = history_id
        count = 0
        for message_id in ids:
            if limit is not None and count >= limit:
                break
            payload = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )
            raw = base64.urlsafe_b64decode(payload["raw"].encode("ascii"))
            newest_history = max(int(payload.get("historyId", 0)), int(newest_history or 0))
            yield parse_message(
                raw,
                provider=self.provider,
                account=self.name,
                uid=message_id,
                thread_id=payload.get("threadId"),
                own_addresses=own,
            )
            count += 1
        if newest_history:
            self._cursor = {"history_id": str(newest_history)}

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

    def _ids_since(self, service: Any, history_id: str) -> Iterator[str]:
        page: str | None = None
        while True:
            try:
                response = (
                    service.users()
                    .history()
                    .list(userId="me", startHistoryId=history_id, pageToken=page)
                    .execute()
                )
            except Exception as exc:  # history expires after ~a week of inactivity
                raise IngestError(
                    "Gmail history cursor expired; run a full re-sync with --reset"
                ) from exc
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    yield added["message"]["id"]
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
