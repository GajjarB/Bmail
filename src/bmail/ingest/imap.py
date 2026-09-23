"""Generic IMAP source — Yahoo, GMX, Web.de, university mail, iCloud, self-hosted.

Uses the standard library only. Incremental sync tracks `UIDVALIDITY` plus the
highest UID seen: if the server changes `UIDVALIDITY` the old UIDs are
meaningless and the folder must be re-scanned, which is the single most common
way naive IMAP sync silently loses mail.

Credentials are never written to the config file. Pass them in the environment
or store them in the OS keychain (see `bmail.secrets`).
"""

from __future__ import annotations

import imaplib
import ssl
from collections.abc import Iterator
from typing import Any

from bmail.errors import IngestError
from bmail.ingest.raw import parse_message
from bmail.models import Message

_BATCH = 200
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ImapSource:
    provider = "imap"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        name: str = "imap",
        port: int = 993,
        folder: str = "INBOX",
        own_addresses: set[str] | None = None,
        use_ssl: bool = True,
    ) -> None:
        if not password:
            raise IngestError(
                f"no password for IMAP account '{name}'. Most providers now require an "
                "app password with two-factor authentication enabled."
            )
        if not use_ssl and host not in LOOPBACK:
            # Plain IMAP sends the password in the clear. That is acceptable only
            # to a bridge on this machine (Proton Mail Bridge, DavMail), never
            # across a network.
            raise IngestError(
                f"IMAP account '{name}': ssl = false is only allowed for a server on "
                "this machine (127.0.0.1 or localhost)"
            )
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.name, self.folder = name, folder
        self.use_ssl = use_ssl
        self.own_addresses = own_addresses or {username.lower()}
        self._cursor: dict[str, Any] = {}

    def _connect(self) -> imaplib.IMAP4:
        try:
            if self.use_ssl:
                conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    self.host, self.port, ssl_context=ssl.create_default_context()
                )
            else:  # loopback only, enforced in __init__
                conn = imaplib.IMAP4(self.host, self.port)
            conn.login(self.username, self.password)
            return conn
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise IngestError(f"IMAP login to {self.host} failed: {exc}") from exc

    def fetch(
        self, cursor: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> Iterator[Message]:
        cursor = dict(cursor or {})
        conn = self._connect()
        try:
            status, data = conn.select(_quoted(self.folder), readonly=True)
            if status != "OK":
                raise IngestError(f"cannot select folder {self.folder}: {data!r}")
            validity = self._uidvalidity(conn, self.folder)
            start_uid = 1
            if cursor.get("uidvalidity") == validity:
                start_uid = int(cursor.get("uidnext", 1))
            else:
                cursor = {}  # UIDVALIDITY changed: every stored UID is now meaningless

            status, data = conn.uid("SEARCH", None, f"UID {start_uid}:*")
            if status != "OK":
                raise IngestError(f"UID SEARCH failed: {data!r}")
            uids = [u.decode() for u in (data[0] or b"").split()]
            uids = [u for u in uids if int(u) >= start_uid]
            if limit:
                uids = uids[:limit]

            # One below where this run starts, so that a run which finds nothing
            # leaves the cursor exactly where it was. Starting from start_uid
            # advanced it by one on every quiet sync, and the next message to
            # arrive — assigned precisely that UID — fell below the cursor and
            # was never fetched.
            highest = start_uid - 1
            for batch_start in range(0, len(uids), _BATCH):
                batch = uids[batch_start : batch_start + _BATCH]
                status, chunk = conn.uid("FETCH", ",".join(batch), "(RFC822)")
                if status != "OK":
                    raise IngestError(f"UID FETCH failed: {chunk!r}")
                for item in chunk:
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue
                    header, raw = item[0], item[1]
                    uid = self._uid_from(header)
                    if uid:
                        highest = max(highest, int(uid))
                    yield parse_message(
                        raw,
                        provider=self.provider,
                        account=self.name,
                        uid=uid,
                        folder=self.folder,
                        own_addresses=self.own_addresses,
                    )
            self._cursor = {"uidvalidity": validity, "uidnext": highest + 1}
        finally:
            try:
                conn.logout()
            except Exception:  # pragma: no cover - best effort
                pass

    @staticmethod
    def _uidvalidity(conn: imaplib.IMAP4, folder: str) -> str:
        """Read UIDVALIDITY from the SELECT that just ran.

        Every server reports it there, as an untagged `OK [UIDVALIDITY n]`.
        Asking with STATUS is the fallback only: RFC 3501 says a client SHOULD
        NOT issue STATUS on the mailbox it has selected, some servers refuse,
        and an empty answer here means a renumbered mailbox goes unnoticed.
        """
        _, found = conn.response("UIDVALIDITY")
        if found and found[0]:
            value = found[0].decode(errors="replace") if isinstance(found[0], bytes) else str(found[0])
            return value.strip()
        status, data = conn.status(_quoted(folder), "(UIDVALIDITY)")
        if status == "OK" and data and data[0]:
            text = data[0].decode(errors="replace")
            if "UIDVALIDITY" in text:
                return text.split("UIDVALIDITY")[1].strip(" ()").split()[0]
        return ""

    @staticmethod
    def _uid_from(header: bytes | str) -> str | None:
        text = header.decode(errors="replace") if isinstance(header, bytes) else header
        if "UID" not in text:
            return None
        after = text.split("UID", 1)[1].strip()
        digits = ""
        for char in after:
            if char.isdigit():
                digits += char
            elif digits:
                break
        return digits or None

    def cursor(self) -> dict[str, Any]:
        return self._cursor


def _quoted(folder: str) -> str:
    """imaplib sends a mailbox name as given, so "Sent Items" would arrive as two
    arguments. Quote anything that is not a bare atom."""
    if folder.startswith('"') or not any(c in folder for c in ' ()"\\{%*'):
        return folder
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
