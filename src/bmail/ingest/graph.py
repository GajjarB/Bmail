"""Microsoft Graph source — Outlook.com, Hotmail, Live and Microsoft 365.

Basic authentication is retired for these mailboxes, and IMAP works only with
XOAUTH2, so Graph is simply the easier path. Auth is MSAL public-client
(authorization code + PKCE, or device code for a headless machine); publisher
verification is free, and — because no mail content leaves the machine — this
stays a client-side integration with no shared server holding anyone's tokens.

Three decisions shape this file:

* **Delta for change tracking, MIME for content.** `/messages/delta` gives a
  durable resume token but a JSON projection of the mail; `$value` gives the
  original RFC 5322 bytes. Fetching the MIME and handing it to the shared
  normaliser means Outlook mail is parsed by exactly the same code as an mbox,
  so quirks cannot leak downstream. A JSON fallback covers the items whose MIME
  Graph will not serve.
* **A truncated run does not advance the cursor.** If `--limit` stops the
  fetch part-way, the old delta link is kept, because storing the new one would
  silently skip everything not yet read.
* **Scopes stay minimal.** `Mail.Read` by default, `Mail.ReadWrite` only when
  category write-back is enabled. The account address comes from the ID token
  rather than requesting `User.Read` for it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from bmail.errors import IngestError
from bmail.ingest.raw import parse_message, synth_message_id
from bmail.models import Attachment, Direction, EmailAddress, Message
from bmail.text.clean import clean_body, detect_language, normalise_whitespace
from bmail.text.html import html_to_text

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com"

READ_SCOPE = "https://graph.microsoft.com/Mail.Read"
MODIFY_SCOPE = "https://graph.microsoft.com/Mail.ReadWrite"

# Delta only needs enough to identify a message; the body arrives as MIME.
DELTA_SELECT = "id,conversationId,internetMessageId,receivedDateTime,sentDateTime,isDraft"

# The fallback path has to reconstruct a whole message from JSON.
FULL_SELECT = (
    "id,conversationId,internetMessageId,subject,from,sender,toRecipients,ccRecipients,"
    "replyTo,receivedDateTime,sentDateTime,body,bodyPreview,hasAttachments,isDraft,"
    "internetMessageHeaders"
)

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_SLEEP = 60.0

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class DeltaExpired(IngestError):
    """Graph dropped the sync state; the folder has to be read from scratch."""


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise IngestError(f"cannot reach Microsoft Graph: {exc.reason}") from exc
    except TimeoutError as exc:
        raise IngestError("Microsoft Graph timed out") from exc


class GraphHTTP:
    """Thin Graph client: bearer auth, throttling, and useful error messages.

    Graph throttles hard and says so precisely, via 429 plus `Retry-After`.
    Honouring that header is not optional — ignoring it is how a backfill gets
    an account temporarily blocked.
    """

    def __init__(
        self,
        token_provider: Callable[[bool], str],
        *,
        transport: Transport | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport or _urllib_transport
        self.max_retries = max_retries
        self._sleep = sleep

    def _send(
        self, method: str, url: str, *, body: bytes | None = None, accept: str = "application/json"
    ) -> tuple[int, dict[str, str], bytes]:
        force_refresh = False
        for attempt in range(self.max_retries + 1):
            headers = {
                "Authorization": f"Bearer {self._token_provider(force_refresh)}",
                "Accept": accept,
                "User-Agent": "Bmail/0.1 (local email triage)",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            status, response_headers, payload = self._transport(method, url, headers, body)

            if status == 401 and not force_refresh:
                force_refresh = True  # token expired mid-run; refresh once and retry
                continue
            if status in _RETRY_STATUSES and attempt < self.max_retries:
                self._sleep(self._backoff(response_headers, attempt))
                continue
            return status, response_headers, payload
        return status, response_headers, payload

    @staticmethod
    def _backoff(headers: dict[str, str], attempt: int) -> float:
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_RETRY_SLEEP)
            except ValueError:
                pass
        return min(2.0**attempt, _MAX_RETRY_SLEEP)

    def json(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, _headers, raw = self._send(method, url, body=body)
        if status == 204 or not raw:
            return {}
        if status >= 400:
            raise self._error(status, raw, url)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngestError(f"Graph returned unreadable JSON from {_short(url)}") from exc
        return decoded if isinstance(decoded, dict) else {"value": decoded}

    def raw(self, url: str) -> bytes | None:
        """Fetch bytes; `None` when Graph will not serve this item as MIME."""
        status, _headers, payload = self._send("GET", url, accept="*/*")
        if status in (400, 404, 406, 415):
            return None
        if status >= 400:
            raise self._error(status, payload, url)
        return payload

    @staticmethod
    def _error(status: int, payload: bytes, url: str) -> IngestError:
        detail = payload.decode("utf-8", errors="replace")[:400]
        code = ""
        try:
            code = str(json.loads(detail or "{}").get("error", {}).get("code", ""))
        except json.JSONDecodeError:
            pass
        if status == 410 or code in ("syncStateNotFound", "resyncRequired"):
            return DeltaExpired(
                "the Graph delta cursor is no longer valid; re-run with --reset for a full sync"
            )
        if status == 403:
            return IngestError(
                f"Graph refused the request ({code or 'forbidden'}). The app registration "
                "probably lacks the scope this needs — check Mail.Read, and Mail.ReadWrite "
                "if you enabled write-back."
            )
        return IngestError(f"Graph HTTP {status} on {_short(url)}: {detail}")


def _short(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return parsed.path or url


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class GraphAuth:
    """MSAL public-client auth with a token cache on disk.

    No client secret: a desktop app cannot keep one, so this is the public
    client + PKCE flow. `device` mode exists because the interactive flow needs
    a browser and a loopback redirect, which a server or an SSH session has not
    got.
    """

    def __init__(
        self,
        client_id: str,
        *,
        tenant: str = "common",
        token_cache_file: str | Path,
        scopes: list[str],
        mode: str = "interactive",
    ) -> None:
        self.client_id = client_id
        self.tenant = tenant
        self.token_cache_file = Path(token_cache_file).expanduser()
        self.scopes = scopes
        self.mode = mode
        self._app: Any = None
        self._cache: Any = None
        self._claims: dict[str, Any] = {}

    def _application(self) -> Any:
        if self._app is not None:
            return self._app
        try:
            import msal
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise IngestError(
                "Microsoft Graph support needs the optional extra: "
                "pip install 'bmail[graph]'"
            ) from exc
        self._cache = msal.SerializableTokenCache()
        if self.token_cache_file.exists():
            try:
                self._cache.deserialize(self.token_cache_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass  # a corrupt cache costs one sign-in, not a failed run
        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=f"{AUTHORITY}/{self.tenant}",
            token_cache=self._cache,
        )
        return self._app

    def token(self, force_refresh: bool = False) -> str:
        app = self._application()
        accounts = app.get_accounts()
        result = None
        if accounts:
            result = app.acquire_token_silent(
                self.scopes, account=accounts[0], force_refresh=force_refresh
            )
        if not result:
            result = self._sign_in(app)
        if "access_token" not in result:
            raise IngestError(
                "Microsoft sign-in failed: "
                f"{result.get('error_description') or result.get('error') or 'unknown error'}"
            )
        self._claims = result.get("id_token_claims") or self._claims
        self._save_cache()
        return str(result["access_token"])

    def _sign_in(self, app: Any) -> dict[str, Any]:
        if self.mode == "device":
            flow = app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise IngestError(
                    "could not start the device-code flow; check that the app registration "
                    "allows public client flows"
                )
            print(flow["message"], flush=True)
            return dict(app.acquire_token_by_device_flow(flow))
        try:
            return dict(app.acquire_token_interactive(scopes=self.scopes))
        except Exception as exc:
            raise IngestError(
                f"interactive sign-in failed ({exc}). On a machine with no browser, set "
                "auth_mode = \"device\" on the account."
            ) from exc

    def _save_cache(self) -> None:
        if self._cache is None or not self._cache.has_state_changed:
            return
        self.token_cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache_file.write_text(self._cache.serialize(), encoding="utf-8")
        try:
            self.token_cache_file.chmod(0o600)
        except OSError:  # pragma: no cover - some filesystems refuse
            pass

    def address(self) -> str:
        for claim in ("preferred_username", "email", "upn"):
            value = self._claims.get(claim)
            if isinstance(value, str) and "@" in value:
                return value.lower()
        return ""


# --------------------------------------------------------------------------- #
# Connector
# --------------------------------------------------------------------------- #


class GraphSource:
    provider = "graph"

    def __init__(
        self,
        *,
        name: str = "outlook",
        client_id: str = "",
        tenant: str = "common",
        token_cache_file: str | Path = "",
        folder: str = "inbox",
        allow_modify: bool = False,
        own_addresses: set[str] | None = None,
        page_size: int = 50,
        auth_mode: str = "interactive",
        http: GraphHTTP | None = None,
        auth: GraphAuth | None = None,
    ) -> None:
        self.name = name
        self.folder = (folder or "").strip()
        self.allow_modify = allow_modify
        self.scopes = [MODIFY_SCOPE if allow_modify else READ_SCOPE]
        self.own_addresses = {a.lower() for a in (own_addresses or set())}
        self.page_size = max(1, min(int(page_size), 500))
        self._cursor: dict[str, Any] = {}

        if http is not None:
            self.auth = auth
            self.http = http
        else:
            if not client_id:
                raise IngestError(
                    f"account '{name}' needs a client_id — register a desktop app in the "
                    "Microsoft Entra portal, see docs/PROVIDERS.md"
                )
            self.auth = auth or GraphAuth(
                client_id,
                tenant=tenant,
                token_cache_file=token_cache_file or f"~/.local/share/bmail/{name}_graph.json",
                scopes=self.scopes,
                mode=auth_mode,
            )
            self.http = GraphHTTP(self.auth.token)

    # -- endpoints ------------------------------------------------------------- #

    def _delta_url(self) -> str:
        base = (
            f"{GRAPH}/me/mailFolders/{urllib.parse.quote(self.folder)}/messages/delta"
            if self.folder
            else f"{GRAPH}/me/messages/delta"
        )
        return f"{base}?$select={DELTA_SELECT}&$top={self.page_size}"

    def address(self) -> str:
        """The mailbox owner, used to tell inbound from outbound.

        Taken from the ID token so that `User.Read` never has to be requested;
        `/me` is only tried as a fallback and its failure is not fatal.
        """
        if self.auth is not None:
            address = self.auth.address()
            if address:
                return address
        try:
            profile = self.http.json("GET", f"{GRAPH}/me?$select=mail,userPrincipalName")
        except IngestError:
            return ""
        value = profile.get("mail") or profile.get("userPrincipalName") or ""
        return str(value).lower()

    # -- fetch -------------------------------------------------------------------- #

    def fetch(
        self, cursor: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> Iterator[Message]:
        cursor = dict(cursor or {})
        previous_link = cursor.get("delta_link") if cursor.get("folder") == self.folder else None

        own = set(self.own_addresses)
        address = self.address()
        if address:
            own.add(address)

        url = previous_link or self._delta_url()
        delta_link: str | None = None
        truncated = False
        count = 0

        while url:
            try:
                page = self.http.json("GET", url)
            except DeltaExpired:
                if url == previous_link:
                    # The stored cursor is dead; start the folder again rather
                    # than failing the run.
                    url = self._delta_url()
                    previous_link = None
                    continue
                raise

            for item in page.get("value", []):
                if "@removed" in item:
                    continue  # Bmail never deletes; a removal is simply not news
                if limit is not None and count >= limit:
                    truncated = True
                    break
                message = self._to_message(item, own)
                if message is not None:
                    yield message
                    count += 1

            if truncated:
                break
            delta_link = page.get("@odata.deltaLink") or delta_link
            url = page.get("@odata.nextLink") or ""

        # A run cut short by --limit must not advance the cursor: storing the new
        # delta link would silently skip everything that was never read.
        if truncated:
            self._cursor = dict(cursor) if previous_link else {}
        elif delta_link:
            self._cursor = {"delta_link": delta_link, "folder": self.folder}

    def _to_message(self, item: dict[str, Any], own: set[str]) -> Message | None:
        item_id = str(item.get("id") or "")
        if not item_id:
            return None
        raw = self.http.raw(f"{GRAPH}/me/messages/{urllib.parse.quote(item_id)}/$value")
        if raw:
            return parse_message(
                raw,
                provider=self.provider,
                account=self.name,
                uid=item_id,
                folder=self.folder or None,
                thread_id=item.get("conversationId") or None,
                own_addresses=own,
            )
        # Graph will not serve MIME for every item it lists (drafts and some
        # calendar-backed items among them). Rebuild from the JSON projection.
        return self._from_json(item_id, own)

    def cursor(self) -> dict[str, Any]:
        return self._cursor

    # -- JSON fallback ---------------------------------------------------------- #

    def _from_json(self, item_id: str, own: set[str]) -> Message | None:
        payload = self.http.json(
            "GET", f"{GRAPH}/me/messages/{urllib.parse.quote(item_id)}?$select={FULL_SELECT}"
        )
        if not payload:
            return None
        return self.build_message(
            payload,
            provider=self.provider,
            account=self.name,
            folder=self.folder or None,
            own_addresses=own,
            attachments=self._attachments(item_id) if payload.get("hasAttachments") else [],
        )

    def _attachments(self, item_id: str) -> list[Attachment]:
        try:
            payload = self.http.json(
                "GET",
                f"{GRAPH}/me/messages/{urllib.parse.quote(item_id)}/attachments"
                "?$select=name,contentType,size",
            )
        except IngestError:
            return []
        return [
            Attachment(
                filename=str(entry.get("name") or "unnamed"),
                content_type=str(entry.get("contentType") or ""),
                size=int(entry.get("size") or 0),
            )
            for entry in payload.get("value", [])
        ]

    @staticmethod
    def build_message(
        payload: dict[str, Any],
        *,
        provider: str = "graph",
        account: str = "outlook",
        folder: str | None = None,
        own_addresses: set[str] | None = None,
        attachments: list[Attachment] | None = None,
    ) -> Message:
        """Canonical `Message` from a Graph JSON message resource."""
        own = {a.lower() for a in (own_addresses or set())}
        sender = _address(payload.get("from") or payload.get("sender"))
        to = _addresses(payload.get("toRecipients"))
        cc = _addresses(payload.get("ccRecipients"))
        reply_to = _addresses(payload.get("replyTo"))

        body = payload.get("body") or {}
        content = str(body.get("content") or payload.get("bodyPreview") or "")
        is_html = str(body.get("contentType") or "").lower() == "html"
        body_raw = normalise_whitespace(html_to_text(content) if is_html else content)
        body_clean = clean_body(body_raw)

        headers = _headers(payload.get("internetMessageHeaders"))
        subject = normalise_whitespace(str(payload.get("subject") or ""))
        when = _timestamp(payload.get("receivedDateTime") or payload.get("sentDateTime"))

        message_id = str(payload.get("internetMessageId") or "").strip()
        if not message_id.startswith("<"):
            message_id = synth_message_id(sender.address, when, subject, body_clean)

        direction = (
            Direction.outbound if own and sender.address.lower() in own else Direction.inbound
        )
        references = [
            token for token in (headers.get("references") or "").split() if token.startswith("<")
        ]
        return Message(
            message_id=message_id,
            provider=provider,
            account=account,
            uid=str(payload.get("id") or "") or None,
            folder=folder,
            thread_id=payload.get("conversationId") or (references[0] if references else message_id),
            in_reply_to=headers.get("in-reply-to") or None,
            references=references,
            **{"from": sender},
            to=to,
            cc=cc,
            reply_to=reply_to[0] if reply_to else None,
            date=when,
            subject=subject,
            list_id=headers.get("list-id"),
            list_unsubscribe=headers.get("list-unsubscribe"),
            precedence=headers.get("precedence"),
            auto_submitted=headers.get("auto-submitted"),
            x_mailer=headers.get("x-mailer"),
            auth_results=headers.get("authentication-results"),
            return_path=headers.get("return-path"),
            has_html=is_html,
            language=detect_language(f"{subject}\n{body_clean}"),
            attachments=list(attachments or []),
            direction=direction,
            headers=headers,
            body_text=body_clean,
            body_raw_text=body_raw,
        )

    # -- write-back ---------------------------------------------------------------- #

    def apply_label(self, message_id: str, label: str) -> None:
        """Add one Outlook category. Never deletes, never moves, never sends.

        Graph has no labels; categories are the closest equivalent and are
        additive. Commas are stripped because Outlook stores categories as a
        comma-delimited list and a comma inside a name splits it in two.
        """
        if not self.allow_modify:
            raise IngestError("category write-back needs allow_modify = true on the account")
        category = label.replace(",", " ").strip()
        if not category:
            return
        quoted = urllib.parse.quote(message_id)
        current = self.http.json("GET", f"{GRAPH}/me/messages/{quoted}?$select=categories")
        existing = [str(value) for value in (current.get("categories") or [])]
        if category in existing:
            return
        self.http.json(
            "PATCH", f"{GRAPH}/me/messages/{quoted}", {"categories": [*existing, category]}
        )


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #


def _address(node: Any) -> EmailAddress:
    if not isinstance(node, dict):
        return EmailAddress()
    inner = node.get("emailAddress") if isinstance(node.get("emailAddress"), dict) else node
    return EmailAddress(
        name=normalise_whitespace(str(inner.get("name") or "")),
        address=str(inner.get("address") or "").strip().lower(),
    )


def _addresses(nodes: Any) -> list[EmailAddress]:
    if not isinstance(nodes, list):
        return []
    found = [_address(node) for node in nodes]
    return [entry for entry in found if entry.address]


def _headers(nodes: Any) -> dict[str, str]:
    """Graph returns internet headers as a name/value list, when it returns them."""
    from bmail.ingest.raw import KEEP_HEADERS

    if not isinstance(nodes, list):
        return {}
    keep = set(KEEP_HEADERS) | {"references", "in-reply-to"}
    out: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or "").lower()
        if name in keep:
            out[name] = normalise_whitespace(str(node.get("value") or ""))
    return out


def _timestamp(value: Any) -> Any:
    from datetime import datetime, timezone

    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)
