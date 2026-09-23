"""Keeping mail up to date while the app is open.

Two ways mail arrives on its own:

- **Polling**, for every live account: a check on a fixed interval, set in
  Setup and stored in the database, so it survives a restart.
- **IMAP IDLE**, for IMAP accounts whose server supports it: one connection
  per account parked in IDLE, which the server wakes the moment a message
  lands. That triggers a check of that account alone, a couple of seconds
  later so a burst of mail becomes one check rather than five.

Every check is an ordinary job on the single worker, so it queues behind
whatever the person is doing instead of racing it; a check that falls due
while the worker is busy waits for it. Nothing here ever opens a sign-in
window: an account whose token cannot be refreshed silently is marked as
needing sign-in, and the person does that from the button, on purpose.
"""

from __future__ import annotations

import itertools
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from bmail.errors import BmailError, IngestError
from bmail.store.db import connect, get_meta, set_meta

if TYPE_CHECKING:  # pragma: no cover
    import imaplib

    from bmail.api import BmailApp
    from bmail.jobs import Job

INTERVAL_KEY = "sync.interval_minutes"
DEFAULT_MINUTES = 5
CHOICES = (0, 2, 5, 15, 30, 60)
FILE_PROVIDERS = {"file", "mbox", "maildir", "eml"}

# RFC 2177 has servers drop an idle client after 30 minutes, and asks clients
# to re-issue IDLE at least every 29. Reconnecting every 20 leaves margin for
# servers and NAT boxes that are less patient than the RFC.
IDLE_RENEW_SECONDS = 20 * 60
_NEW_MAIL = re.compile(rb"^\* \d+ EXISTS\b", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


@dataclass
class AccountState:
    name: str
    provider: str
    status: str = "waiting"  # waiting | ok | sign-in | error
    error: str = ""
    last_ok: datetime | None = None


class Syncer:
    """Schedules background checks for one running app."""

    def __init__(
        self,
        app: BmailApp,
        *,
        first_delay: float = 3.0,
        debounce: float = 2.0,
        busy_retry: float = 5.0,
        use_llm: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.first_delay = first_delay
        self.debounce = debounce
        self.busy_retry = busy_retry
        self.use_llm = use_llm
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchers: dict[str, IdleWatcher] = {}
        self._pokes: dict[str, float] = {}
        self._due = 0.0
        self._blocked_until = 0.0
        self.minutes = self._load_minutes()
        self.generation = 0
        self.last_run: datetime | None = None
        self.last_kind = ""
        self.last: dict[str, Any] = {}
        self.accounts: dict[str, AccountState] = {}

    # -- configuration ----------------------------------------------------------- #

    @property
    def interval(self) -> float:
        return self.minutes * 60.0

    def live_accounts(self) -> list[dict[str, Any]]:
        """Configured accounts that can receive mail. A file does not."""
        return [
            entry for entry in self.app.config.accounts
            if str(entry.get("provider", "")).lower() not in FILE_PROVIDERS
        ]

    def _load_minutes(self) -> int:
        conn = connect(self.app.config.database, read_only=True)
        try:
            stored = get_meta(conn, INTERVAL_KEY)
        finally:
            conn.close()
        try:
            return int(stored) if stored is not None else DEFAULT_MINUTES
        except ValueError:
            return DEFAULT_MINUTES

    def set_interval(self, minutes: Any) -> None:
        try:
            value = int(minutes)
        except (TypeError, ValueError) as exc:
            raise BmailError("interval must be a whole number of minutes") from exc
        if value not in CHOICES:
            raise BmailError(f"interval must be one of {', '.join(map(str, CHOICES))} minutes")
        conn = connect(self.app.config.database)
        try:
            set_meta(conn, INTERVAL_KEY, str(value))
        finally:
            conn.close()
        with self._lock:
            was_off = self.minutes == 0
            self.minutes = value
            now = self._clock()
            # Switching on checks straight away; changing the interval counts
            # from now, not from whenever the last check happened to run.
            self._due = now + (self.first_delay if was_off else self.interval)
            if self.running:
                self._arrange_watchers()
        self._wake.set()

    # -- lifecycle ------------------------------------------------------------------ #

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._due = self._clock() + self.first_delay
        self._thread = threading.Thread(target=self._run, name="bmail-sync", daemon=True)
        self._thread.start()
        with self._lock:
            self._arrange_watchers()

    def stop(self, timeout: float = 3.0) -> None:
        self._stopping.set()
        self._wake.set()
        with self._lock:
            watchers, self._watchers = list(self._watchers.values()), {}
        for watcher in watchers:
            watcher.stop(timeout=timeout)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def poke(self, account: str) -> None:
        """New mail was announced for `account`; check it shortly."""
        with self._lock:
            self._pokes.setdefault(account, self._clock())
        self._wake.set()

    # -- the loop --------------------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(self._next_wait())
            self._wake.clear()
            if self._stopping.is_set():
                return
            try:
                self._tick()
            except Exception:  # pragma: no cover - the loop must outlive one bad tick
                pass

    def _next_wait(self) -> float:
        with self._lock:
            if self.minutes <= 0:
                return 60.0
            now = self._clock()
            targets = [self._due] + [t + self.debounce for t in self._pokes.values()]
            target = max(min(targets), self._blocked_until)
            return min(max(target - now, 0.05), 60.0)

    def _tick(self) -> None:
        with self._lock:
            if self.minutes <= 0:
                self._pokes.clear()
                return
            now = self._clock()
            due = now >= self._due
            poked = [name for name, at in self._pokes.items() if now - at >= self.debounce]
            if not (due or poked) or now < self._blocked_until:
                return
            if self.app.jobs.active() is not None:
                # One writer. Wait for the worker rather than queue behind it:
                # a queue of stale checks would run back to back afterwards.
                self._blocked_until = now + self.busy_retry
                return
            live = self.live_accounts()
            if due:
                entries = live
                self._pokes.clear()
                self._due = now + self.interval
            else:
                entries = [e for e in live if e.get("name") in poked]
                for name in poked:
                    self._pokes.pop(name, None)
        if entries:
            self.submit(entries)

    def submit(self, entries: list[dict[str, Any]]) -> Job:
        config = self.app.config

        def work(job: Job) -> dict[str, Any]:
            specs: list[dict[str, Any]] = []
            unresolved: dict[str, str] = {}
            for entry in entries:
                try:
                    specs.append(config.resolved_account(entry))
                except BmailError as exc:
                    unresolved[str(entry.get("name", "?"))] = str(exc)
            outcome = self.app.sync(
                job, specs, interactive=False, strict=False, use_llm=self.use_llm
            ) if specs else {"fetched": {}, "new": {}, "errors": {}, "sign_in": []}
            outcome["errors"].update(unresolved)
            self.record(outcome, kind="sync")
            return outcome

        return self.app.jobs.submit("sync", work)

    # -- results ------------------------------------------------------------------------ #

    def record(self, outcome: dict[str, Any], *, kind: str) -> None:
        """Note a finished check, automatic or not.

        `generation` moves only when something changed, so a page polling
        status can tell "new mail arrived" from "a check ran".
        """
        with self._lock:
            now = _now()
            self.last_run, self.last_kind = now, kind
            fresh = sum((outcome.get("new") or {}).values())
            if fresh or outcome.get("sorted"):
                self.generation += 1
            self.last = {
                "new": fresh,
                "sorted": int(outcome.get("sorted") or 0),
                "errors": dict(outcome.get("errors") or {}),
            }
            providers = {str(e.get("name")): str(e.get("provider", "")) for e in self.live_accounts()}
            for name in outcome.get("fetched") or {}:
                state = self._state(name, providers)
                state.status, state.error, state.last_ok = "ok", "", now
            sign_in = set(outcome.get("sign_in") or [])
            for name, error in (outcome.get("errors") or {}).items():
                state = self._state(name, providers)
                state.status = "sign-in" if name in sign_in else "error"
                state.error = error
            if kind != "sync" and self.minutes > 0:
                # Someone just checked by hand; the next automatic check can wait
                # a full interval rather than repeat it a minute later.
                self._due = self._clock() + self.interval

    def _state(self, name: str, providers: dict[str, str]) -> AccountState:
        if name not in self.accounts:
            self.accounts[name] = AccountState(name, providers.get(name, ""))
        return self.accounts[name]

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self.app.jobs.active()
            accounts = []
            for entry in self.live_accounts():
                name = str(entry.get("name", "?"))
                state = self.accounts.get(name) or AccountState(name, str(entry.get("provider", "")))
                watcher = self._watchers.get(name)
                if self.minutes <= 0:
                    mode = "off"
                elif watcher is not None and watcher.mode == "idle":
                    mode = "push"
                else:
                    mode = "poll"
                accounts.append({
                    "name": name,
                    "provider": str(entry.get("provider", "")),
                    "mode": mode,
                    "status": state.status,
                    "error": state.error,
                    "last_ok": _iso(state.last_ok),
                })
            next_in = None
            if self.running and self.minutes > 0:
                next_in = max(0, round(self._due - self._clock()))
            return {
                "enabled": self.minutes > 0,
                "interval": self.minutes,
                "choices": list(CHOICES),
                "running": self.running,
                "checking": active is not None and active.kind in ("sync", "ingest"),
                "next_in": next_in,
                "last_run": _iso(self.last_run),
                "last_kind": self.last_kind,
                "last": self.last,
                "generation": self.generation,
                "accounts": accounts,
            }

    # -- IMAP IDLE -------------------------------------------------------------------------- #

    def _arrange_watchers(self) -> None:
        """One watcher per IMAP account while syncing is on; none while off."""
        wanted = {}
        if self.minutes > 0 and not self._stopping.is_set():
            wanted = {
                str(e.get("name")): e for e in self.live_accounts()
                if str(e.get("provider", "")).lower() == "imap"
            }
        for name in list(self._watchers):
            if name not in wanted:
                self._watchers.pop(name).stop(timeout=0)
        for name, entry in wanted.items():
            if name not in self._watchers:
                watcher = IdleWatcher(
                    entry,
                    resolve=self.app.config.resolved_account,
                    on_mail=lambda account=name: self.poke(account),
                )
                self._watchers[name] = watcher
                watcher.start()


def _uidnext(conn: imaplib.IMAP4) -> int | None:
    _, found = conn.response("UIDNEXT")
    try:
        return int(found[0]) if found and found[0] else None
    except (TypeError, ValueError):
        return None


class _Renew(Exception):
    """IDLE ran its full term; reconnect and start again."""


class _Unsupported(Exception):
    """The server cannot IDLE; polling covers this account."""


class IdleWatcher:
    """One IMAP connection parked in IDLE, calling `on_mail` when mail lands.

    It never fetches anything itself — it only says "look now", and the check
    runs as a normal job. That keeps a single code path for reading mail and a
    single writer on the store.
    """

    _tags = itertools.count(1)

    def __init__(
        self,
        entry: dict[str, Any],
        *,
        resolve: Callable[[dict[str, Any]], dict[str, Any]],
        on_mail: Callable[[], None],
        renew: float = IDLE_RENEW_SECONDS,
        backoff: tuple[float, float] = (5.0, 900.0),
    ) -> None:
        self.entry = entry
        self.name = str(entry.get("name", "imap"))
        self.resolve = resolve
        self.on_mail = on_mail
        self.renew = renew
        self.backoff = backoff
        self.mode = "starting"  # starting | idle | unsupported | error
        self.error = ""
        self._uidnext: int | None = None
        self._stopping = threading.Event()
        self._conn: imaplib.IMAP4 | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=f"bmail-idle-{self.name}",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 3.0) -> None:
        self._stopping.set()
        with self._lock:
            conn = self._conn
        if conn is not None:
            # A reader blocked in IDLE only wakes when the socket goes away.
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if timeout:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        delay = self.backoff[0]
        while not self._stopping.is_set():
            try:
                self._session()
                delay = self.backoff[0]
            except _Unsupported:
                self.mode = "unsupported"
                return
            except Exception as exc:
                if self._stopping.is_set():
                    return
                self.mode, self.error = "error", str(exc)
                self._stopping.wait(delay)
                delay = min(delay * 2, self.backoff[1])

    def _session(self) -> None:
        from bmail.ingest import build_source
        from bmail.ingest.imap import _quoted

        spec = self.resolve(self.entry)
        source = build_source(spec)
        conn = source._connect()
        with self._lock:
            self._conn = conn
        try:
            if self._stopping.is_set():
                return
            if "IDLE" not in conn.capabilities:
                raise _Unsupported
            status, data = conn.select(_quoted(source.folder), readonly=True)
            if status != "OK":
                raise IngestError(f"cannot select folder {source.folder}: {data!r}")
            # IDLE only reports what arrives while it is running, so mail that
            # landed while this watcher was reconnecting would never be
            # announced. UIDNEXT moves with every new message: if it moved
            # since the last connection, something came in during the gap.
            uidnext = _uidnext(conn)
            if uidnext is not None:
                if self._uidnext is not None and uidnext > self._uidnext:
                    self.on_mail()
                self._uidnext = uidnext
            deadline = time.monotonic() + self.renew
            while not self._stopping.is_set():
                budget = deadline - time.monotonic()
                if budget <= 0:
                    return
                try:
                    arrived = self._idle(conn, budget)
                except _Renew:
                    return
                if arrived:
                    self.on_mail()
        finally:
            with self._lock:
                self._conn = None
            try:
                conn.shutdown()
            except Exception:
                pass

    def _idle(self, conn: imaplib.IMAP4, budget: float) -> bool:
        tag = f"BI{next(self._tags)}".encode()
        conn.send(tag + b" IDLE\r\n")
        reply = conn.readline()
        if not reply.startswith(b"+"):
            raise _Unsupported
        self.mode, self.error = "idle", ""
        conn.sock.settimeout(budget)
        try:
            while True:
                line = conn.readline()
                if not line:
                    raise ConnectionError("the server closed the connection")
                if _NEW_MAIL.match(line):
                    break
                # Anything else — "* OK still here", flag changes, expunges —
                # is not new mail, and IDLE carries on.
        except TimeoutError as exc:
            # A file object that timed out mid-read is in an unknown state, so
            # this connection is finished with rather than reused.
            raise _Renew from exc
        conn.sock.settimeout(None)
        conn.send(b"DONE\r\n")
        while True:
            line = conn.readline()
            if not line:
                raise ConnectionError("the server closed the connection")
            if line.startswith(tag + b" "):
                return True
