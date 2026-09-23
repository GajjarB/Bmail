"""A small, real IMAP server for tests.

Real in the sense that matters: it speaks the protocol over a socket, so the
connector is exercised through Python's own imaplib — literal parsing, tagged
responses, the SELECT untagged data — rather than through a mock of imaplib
that would agree with whatever the connector already assumes.

It implements only what Bmail sends: CAPABILITY, LOGIN, SELECT/EXAMINE,
STATUS, UID SEARCH, UID FETCH, NOOP, IDLE and LOGOUT, over plain TCP.
"""

from __future__ import annotations

import select
import shlex
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from email import message_from_bytes
from email.utils import parsedate_to_datetime


@dataclass
class Mailbox:
    uidvalidity: int = 1000
    messages: list[tuple[int, bytes]] = field(default_factory=list)
    next_uid: int = 1
    changed: threading.Condition = field(default_factory=threading.Condition)

    def add(self, raw: bytes) -> int:
        with self.changed:
            uid = self.next_uid
            self.next_uid += 1
            self.messages.append((uid, raw))
            self.changed.notify_all()
        return uid

    def renumber(self, uidvalidity: int) -> None:
        """What a server does after a mailbox rebuild: new UIDVALIDITY, and the
        same messages under UIDs that no longer mean what they used to."""
        with self.changed:
            self.uidvalidity = uidvalidity
            self.messages = [(index + 1, raw) for index, (_, raw) in enumerate(self.messages)]
            self.next_uid = len(self.messages) + 1


class FakeImapServer:
    def __init__(self, *, user: str = "me@example.com", password: str = "secret",
                 idle: bool = True, refuse_status: bool = False) -> None:
        self.user, self.password = user, password
        self.idle = idle
        # RFC 3501 says a client SHOULD NOT issue STATUS on the selected mailbox,
        # and some servers refuse it outright. This models those servers.
        self.refuse_status = refuse_status
        self.mailbox = Mailbox()
        self.missing_folders: set[str] = set()
        self.log: list[str] = []
        self.logins = 0
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.allow_reuse_address = True
        server.fake = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def __enter__(self) -> FakeImapServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Handler(socketserver.StreamRequestHandler):
    def _w(self, line: str | bytes) -> None:
        self.wfile.write((line.encode() if isinstance(line, str) else line) + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:  # noqa: C901 - a protocol dispatcher is a list of cases
        fake: FakeImapServer = self.server.fake  # type: ignore[attr-defined]
        box = fake.mailbox
        self._w("* OK fake IMAP4rev1 ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            if not text:
                continue
            fake.log.append(text)
            tag, _, rest = text.partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()

            if command == "CAPABILITY":
                self._w("* CAPABILITY IMAP4rev1" + (" IDLE" if fake.idle else ""))
                self._w(f"{tag} OK done")
            elif command == "LOGIN":
                user, password = shlex.split(args)[:2]
                if (user, password) == (fake.user, fake.password):
                    fake.logins += 1
                    self._w(f"{tag} OK logged in")
                else:
                    self._w(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
            elif command in ("SELECT", "EXAMINE"):
                if args.strip('"') in fake.missing_folders:
                    self._w(f"{tag} NO [NONEXISTENT] no such mailbox")
                    continue
                with box.changed:
                    self._w(f"* {len(box.messages)} EXISTS")
                    self._w("* 0 RECENT")
                    self._w(f"* OK [UIDVALIDITY {box.uidvalidity}] UIDs valid")
                    self._w(f"* OK [UIDNEXT {box.next_uid}] next uid")
                mode = "READ-ONLY" if command == "EXAMINE" else "READ-WRITE"
                self._w(f"{tag} OK [{mode}] selected")
            elif command == "STATUS":
                if fake.refuse_status:
                    self._w(f"{tag} NO STATUS on the selected mailbox is not allowed")
                else:
                    self._w(f'* STATUS "INBOX" (UIDVALIDITY {box.uidvalidity})')
                    self._w(f"{tag} OK done")
            elif command == "UID":
                sub, _, sub_args = args.partition(" ")
                if sub.upper() == "SEARCH":
                    self._w("* SEARCH " + " ".join(str(u) for u in self._search(box, sub_args)))
                    self._w(f"{tag} OK search done")
                elif sub.upper() == "FETCH":
                    wanted = self._uid_set(sub_args.split(" ", 1)[0])
                    with box.changed:
                        for seq, (uid, raw) in enumerate(box.messages, start=1):
                            if uid in wanted:
                                self.wfile.write(
                                    f"* {seq} FETCH (UID {uid} RFC822 {{{len(raw)}}}\r\n".encode()
                                )
                                self.wfile.write(raw)
                                self._w(")")
                    self._w(f"{tag} OK fetch done")
                else:
                    self._w(f"{tag} BAD unsupported UID command")
            elif command == "NOOP":
                self._w(f"{tag} OK")
            elif command == "IDLE":
                if not fake.idle:
                    self._w(f"{tag} BAD IDLE not supported")
                    continue
                self._idle(tag, box)
            elif command == "LOGOUT":
                self._w("* BYE logging out")
                self._w(f"{tag} OK bye")
                return
            else:
                self._w(f"{tag} BAD unknown command {command}")

    def _idle(self, tag: str, box: Mailbox) -> None:
        self._w("+ idling")
        with box.changed:
            known = len(box.messages)
        while True:
            with box.changed:
                box.changed.wait(timeout=0.05)
                count = len(box.messages)
            if count > known:
                self._w(f"* {count} EXISTS")
                known = count
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable:
                line = self.rfile.readline()
                if not line:
                    return
                if line.strip().upper() == b"DONE":
                    self._w(f"{tag} OK idle done")
                    return

    @staticmethod
    def _search(box: Mailbox, criteria: str) -> list[int]:
        # "UID a:*", optionally with "SINCE dd-Mon-yyyy". IMAP gives "*" the
        # largest UID in the box, and a range is unordered, so a:* with a past
        # the end still returns the last message — the detail naive clients get
        # wrong. SINCE compares dates only, ignoring the time of day.
        tokens = criteria.split()
        spec = tokens[tokens.index("UID") + 1] if "UID" in tokens else "1:*"
        since = None
        if "SINCE" in tokens:
            since = datetime.strptime(tokens[tokens.index("SINCE") + 1], "%d-%b-%Y").date()
        low_text, _, high_text = spec.partition(":")
        uids = [uid for uid, _ in box.messages]
        if not uids:
            return []
        top = max(uids)
        low = int(low_text)
        high = top if high_text == "*" else int(high_text or low_text)
        low, high = min(low, high), max(low, high)
        dated = {uid: _date_of(raw) for uid, raw in box.messages}
        return [uid for uid in uids if low <= uid <= high
                and (since is None or dated[uid] is None or dated[uid] >= since)]

    @staticmethod
    def _uid_set(text: str) -> set[int]:
        wanted: set[int] = set()
        for part in text.split(","):
            if ":" in part:
                low, high = part.split(":")
                wanted.update(range(int(low), int(high) + 1))
            elif part:
                wanted.add(int(part))
        return wanted


def _date_of(raw: bytes) -> date | None:
    header = message_from_bytes(raw).get("Date")
    try:
        return parsedate_to_datetime(header).date() if header else None
    except (TypeError, ValueError):
        return None
