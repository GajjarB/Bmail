"""The local application server behind the desktop UI.

Loopback only, on a random port, behind a token printed once at startup. Those
three properties are the whole security model, and two more guards make them
hold in a browser: the `Host` header is checked so a rebound DNS name cannot
reach the server, and `Origin` is checked on writes so another page cannot post
to it.

This server used to be strictly read-only, on the argument that ingest and
triage are long-running jobs that belong to a scheduler. The UI makes that
untenable — a person clicking "Triage" should not be told to open a terminal —
so writes are supported now, but never inline: they are queued on the single
worker in `bmail.jobs`, and the UI polls for progress. The handler itself still
does nothing that can take longer than a query.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bmail import __version__
from bmail import accounts as mailboxes
from bmail import secrets as keystore
from bmail.config import Config
from bmail.errors import BmailError, SignInRequired
from bmail.jobs import Job, JobRunner
from bmail.packs import available_packs, load_pack
from bmail.store import repo
from bmail.store.db import connect, get_meta, set_meta
from bmail.sync import Syncer

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
MAX_BODY = 1 << 20  # 1 MiB is far more than any request here needs
PACK_KEY = "app.pack"
SETUP_DONE_KEY = "setup.done"
PREFS_KEY = "ui.prefs"

# Settings the page remembers. They live in the store, not only in the
# browser: a browser keeps its storage per address, and an address that
# changed between starts would forget every one of them.
PREFS = {
    "theme": {"studio", "compact"},
    "scheme": {"system", "light", "dark"},
    "window": {"1", "7", "30", "3650"},
}

# The port Bmail asks for first. A fixed one keeps the page's address the same
# from one start to the next; when something else holds it, any free port does.
DEFAULT_PORT = 47113


# Whether mimetypes knows .woff2 depends on the Python version and the system's
# mime.types file. Without it the bundled fonts go out as octet-stream.
mimetypes.add_type("font/woff2", ".woff2")


def static_dir() -> Path:
    return Path(str(resources.files("bmail.ui").joinpath("static")))


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class ApiError(BmailError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #


class BmailApp:
    """Everything the handler needs, so the handler stays about HTTP only."""

    def __init__(self, config: Config, *, pack: str | None = None) -> None:
        self.config = config
        self.jobs = JobRunner()
        self.token = secrets.token_urlsafe(24)
        config.ensure_home()
        connect(config.database).close()  # create and migrate the store up front
        # What the person chose in the app outranks the config file's default,
        # and a --pack flag outranks both. Kept in the store, not the browser,
        # so background sync sorts with the pack the person is looking at.
        self.pack_name = pack or self._meta(PACK_KEY) or config.pack
        # Built always, started only by `serve`: a server made for a test or a
        # script should not start reaching out to mail servers on its own.
        self.syncer = Syncer(self)

    # -- helpers ---------------------------------------------------------------- #

    def _meta(self, key: str) -> str | None:
        conn = self.read()
        try:
            return get_meta(conn, key)
        finally:
            conn.close()

    def _set_meta(self, key: str, value: str) -> None:
        conn = self.write()
        try:
            set_meta(conn, key, value)
        finally:
            conn.close()

    def pack(self, name: str | None = None):
        return load_pack(name or self.pack_name, tuple(self.config.pack_dirs))

    def read(self) -> sqlite3.Connection:
        return connect(self.config.database, read_only=True)

    def write(self) -> sqlite3.Connection:
        return connect(self.config.database)

    # -- read endpoints ----------------------------------------------------------- #

    def state(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Everything the UI needs to boot: packs, counts, accounts, model status."""
        from bmail.llm.client import OllamaClient, detect_tier

        pack = self.pack(_one(query, "pack"))
        conn = self.read()
        try:
            from bmail.outputs.brief import build_brief

            counts = repo.count_messages(conn)
            stats = repo.verdict_stats(conn, pack.name)
            # What the Today badge shows: requests still open, the same number
            # the Today headline states. The count of every request ever made
            # is history, not something waiting on you.
            needs_you = build_brief(conn, pack, limit=0).counts["needs_you"]
            cases = repo.list_cases(conn, pack.name)
            accounts = [dict(row) for row in repo.list_accounts(conn)]
            runs = repo.recent_runs(conn, 5)
            name = repo.owner_name(conn)
        finally:
            conn.close()
        addresses = [str(entry.get("address") or entry.get("username") or "")
                     for entry in self.config.accounts] + list(self.config.owner_addresses)

        tier = detect_tier()
        model = self.config.llm.model or tier.model
        llm: dict[str, Any] = {"enabled": self.config.llm.enabled, "model": model,
                               "tier": tier.name, "ram_gb": tier.ram_gb, "available": False,
                               "pulled": False, "host": self.config.llm.host}
        if self.config.llm.enabled:
            try:
                client = OllamaClient(
                    self.config.llm.host, allow_remote=self.config.llm.allow_remote
                )
                llm["available"] = client.available()
                llm["pulled"] = llm["available"] and client.has_model(model)
            except BmailError as exc:
                llm["error"] = str(exc)

        return {
            "pack": pack.name,
            "packs": sorted(available_packs(self.config.pack_dirs)),
            # The UI labels itself from this: "Applications" for job-seeker,
            # "Invoices" for freelance-ops. The product should speak the user's
            # domain, not the engine's.
            "entity": pack.entity,
            "entity_label": pack.entity.replace("_", " "),
            "categories": [
                {
                    "name": name,
                    "description": spec.description,
                    "action_required": spec.action_required,
                    "high_stakes": spec.high_stakes,
                }
                for name, spec in pack.categories.items()
            ],
            "states": pack.states,
            "messages": counts,
            "verdicts": stats,
            "needs_you": needs_you,
            "cases": {
                "total": len(cases),
                "open": sum(1 for case in cases if not case.closed),
                "stalled": sum(1 for case in cases if case.stalled),
            },
            "accounts": accounts,
            "configured_accounts": [mailboxes.public(entry) for entry in self.config.accounts],
            # Who the app is for, to greet and to show at the foot of the sidebar.
            "owner": {"name": name, "address": next((a for a in addresses if "@" in a), "")},
            "setup": self.setup_state(counts["total"]),
            "prefs": self.prefs(),
            "runs": runs,
            "llm": llm,
            "version": __version__,
            "home": str(self.config.home),
            "database": str(self.config.database),
        }

    def brief(self, query: dict[str, list[str]]) -> dict[str, Any]:
        from bmail.outputs.brief import build_brief

        conn = self.read()
        try:
            brief = build_brief(
                conn,
                self.pack(_one(query, "pack")),
                window_days=_int(query, "days", 1),
                limit=_int(query, "limit", 8),
            )
            return brief.model_dump(mode="json")
        finally:
            conn.close()

    def cases(self, query: dict[str, list[str]]) -> dict[str, Any]:
        conn = self.read()
        try:
            pack = self.pack(_one(query, "pack"))
            cases = repo.list_cases(
                conn,
                pack.name,
                state=_one(query, "state"),
                stalled=True if _one(query, "stalled") else None,
                open_only=bool(_one(query, "open")),
            )
            reached = repo.states_reached(conn, pack.name)
            return {
                "cases": [
                    {**case.model_dump(mode="json"), "reached": reached.get(case.id or 0, [])}
                    for case in cases
                ],
                "states": pack.states,
            }
        finally:
            conn.close()

    def case(self, case_id: int, query: dict[str, list[str]]) -> dict[str, Any]:
        conn = self.read()
        try:
            pack = self.pack(_one(query, "pack"))
            found = next(
                (c for c in repo.list_cases(conn, pack.name) if c.id == case_id), None
            )
            if found is None:
                raise ApiError(f"no case {case_id}", 404)
            return {
                "case": {
                    **found.model_dump(mode="json"),
                    "reached": repo.states_reached(conn, pack.name).get(case_id, []),
                },
                "timeline": [dict(row) for row in repo.case_timeline(conn, case_id)],
            }
        finally:
            conn.close()

    def messages(self, query: dict[str, list[str]]) -> dict[str, Any]:
        conn = self.read()
        try:
            limit = max(1, min(_int(query, "limit", 50), 200))
            offset = max(0, _int(query, "offset", 0))
            rows, total = repo.browse_messages(
                conn,
                self.pack(_one(query, "pack")).name,
                category=_one(query, "category"),
                action_required=_bool(query, "action"),
                needs_review=_bool(query, "review"),
                lane=_one(query, "lane"),
                query=_one(query, "q") or "",
                limit=limit,
                offset=offset,
            )
            return {"messages": rows, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    def message(self, message_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        conn = self.read()
        try:
            detail = repo.message_detail(conn, message_id, self.pack(_one(query, "pack")).name)
            if detail is None:
                raise ApiError("no such message", 404)
            return detail
        finally:
            conn.close()

    def stats(self, query: dict[str, list[str]]) -> dict[str, Any]:
        conn = self.read()
        try:
            pack = self.pack(_one(query, "pack"))
            return {
                "messages": repo.count_messages(conn),
                "verdicts": repo.verdict_stats(conn, pack.name),
                "categories": repo.category_counts(conn, pack.name),
                "cases": len(repo.list_cases(conn, pack.name)),
            }
        finally:
            conn.close()

    # -- write endpoints ------------------------------------------------------------ #

    def correct(self, message_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """A human correction. Updates the verdict and stores a training row."""
        pack = self.pack(body.get("pack"))
        category = str(body.get("category") or "").strip()
        if category not in pack.categories:
            raise ApiError(f"'{category}' is not a category of pack '{pack.name}'")
        action = body.get("action_required")
        conn = self.write()
        try:
            updated = repo.correct_verdict(
                conn, message_id, pack.name, category,
                None if action is None else bool(action),
            )
        finally:
            conn.close()
        if not updated:
            raise ApiError("that message has not been triaged yet", 404)
        return {"ok": True, "message_id": message_id, "category": category}

    # -- jobs -------------------------------------------------------------------------- #

    def start_triage(self, body: dict[str, Any]) -> Job:
        pack_name = str(body.get("pack") or self.pack_name)
        limit = body.get("limit")
        use_llm = not bool(body.get("no_llm"))
        retriage = bool(body.get("all"))

        def work(job: Job) -> dict[str, Any]:
            conn = self.write()
            try:
                return self._sort(
                    conn, job, pack_name,
                    limit=int(limit) if limit else None, use_llm=use_llm, retriage=retriage,
                )
            finally:
                conn.close()

        return self.jobs.submit("triage", work)

    def start_ingest(self, body: dict[str, Any]) -> Job:
        """Check for new mail, and sort whatever arrived, as one job.

        Sorting is on by default because fetching alone leaves new mail sitting
        unsorted until someone thinks to press a second button. `sort: false`
        keeps the old fetch-only behaviour.
        """
        account = body.get("account")
        path = body.get("path")
        limit = body.get("limit")
        reset = bool(body.get("reset"))
        sort = body.get("sort", True) is not False
        use_llm = not bool(body.get("no_llm"))

        def work(job: Job) -> dict[str, Any]:
            # Resolved inside the job, like every other request error here: the
            # handler only queues, and the job reports what went wrong.
            if path:
                specs = [{"name": account or "local", "provider": "file", "path": path}]
            elif account:
                specs = [self.config.resolved_account(self.config.account(str(account)))]
            else:
                specs = [self.config.resolved_account(e) for e in self.config.accounts]
            if not specs:
                raise ApiError(
                    "no mail account set up yet: add one to config.toml "
                    "(see docs/PROVIDERS.md), or read a file from Setup"
                )
            outcome = self.sync(
                job, specs, limit=int(limit) if limit else None, reset=reset,
                sort=sort, use_llm=use_llm,
            )
            if not path:
                self.syncer.record(outcome, kind="ingest")
            return outcome

        return self.jobs.submit("ingest", work)

    def sync(
        self,
        job: Job,
        specs: list[dict[str, Any]],
        *,
        interactive: bool = True,
        limit: int | None = None,
        reset: bool = False,
        sort: bool = True,
        use_llm: bool = True,
        strict: bool = True,
    ) -> dict[str, Any]:
        """Fetch from each account, then sort what is new. Runs inside a job.

        One account failing — an expired sign-in, a server that is down — does
        not stop the others: its error is reported against its name and the
        rest still sync. When every account fails the job fails too, unless
        `strict` is off, which the background sync uses so it can still show
        each account's error against its name.
        """
        from bmail.ingest import build_source, ingest

        conn = self.write()
        try:
            own = set(self.config.owner_addresses) | repo.own_addresses(conn)
            fetched: dict[str, int] = {}
            new: dict[str, int] = {}
            errors: dict[str, str] = {}
            sign_in: list[str] = []
            fetch_share = 0.5 if sort else 1.0
            for index, spec in enumerate(specs):
                job.raise_if_cancelled()
                name = str(spec.get("name", "?"))
                job.update(step=f"checking {name}", progress=fetch_share * index / len(specs))
                try:
                    source = build_source(spec, own_addresses=own, interactive=interactive)
                    result = ingest(conn, source, limit=limit, resume=not reset)
                except SignInRequired as exc:
                    conn.rollback()
                    errors[name] = str(exc)
                    sign_in.append(name)
                    continue
                except BmailError as exc:
                    conn.rollback()
                    errors[name] = str(exc)
                    continue
                fetched[name] = result.fetched
                new[name] = result.new
            if strict and errors and not fetched:
                raise ApiError("; ".join(f"{name}: {error}" for name, error in errors.items()))

            outcome: dict[str, Any] = {
                "fetched": fetched,
                "new": new,
                "errors": errors,
                "sign_in": sign_in,
                "store": repo.count_messages(conn),
            }
            if sort:
                sorted_ = self._sort(
                    conn, job, self.pack_name, use_llm=use_llm, span=(fetch_share, 1.0)
                )
                outcome["sorted"] = int(sorted_.get("processed", 0))
                outcome["triage"] = sorted_
            return outcome
        finally:
            conn.close()

    def _sort(
        self,
        conn: sqlite3.Connection,
        job: Job,
        pack_name: str,
        *,
        limit: int | None = None,
        use_llm: bool = True,
        retriage: bool = False,
        span: tuple[float, float] = (0.0, 1.0),
    ) -> dict[str, Any]:
        """Run the pipeline over messages this pack has not seen, inside a job.

        `span` is the slice of the job's progress bar this stage owns, so a
        job that fetches first and sorts second fills one bar, not two.
        """
        from bmail.pipeline import build_pipeline

        low, high = span
        pack = self.pack(pack_name)
        job.update(step="loading messages", progress=low + 0.02 * (high - low))
        messages = repo.iter_messages(
            conn, limit=limit, only_untriaged_for=None if retriage else pack.name
        )
        if not messages:
            # Checked before the pipeline is built: building it loads the model
            # and asks the local LLM what it is, which a quiet sync can skip.
            return {"processed": 0, "notes": ["nothing new to triage"]}
        pipeline, notes = build_pipeline(conn, pack, self.config, use_llm=use_llm)

        total = len(messages)

        def progress(index: int, _total: int, _message: Any) -> None:
            job.raise_if_cancelled()
            job.update(progress=low + (high - low) * index / total, step=f"sorting {index} of {total}")

        stats = pipeline.triage(conn, messages, progress=progress)
        job.update(step="training the classifier", progress=low + 0.99 * (high - low))
        trained: dict[str, Any] = {}
        if pipeline.classifier is not None and self.config.classifier.distil_after_backfill:
            trained = pipeline.classifier.train(conn)
            if trained.get("trained"):
                pipeline.classifier.save(str(self.config.model_path))
        return {**stats.as_dict(), "notes": notes, "classifier": trained}

    def start_export(self, body: dict[str, Any]) -> Job:
        kinds = [str(k) for k in (body.get("kinds") or ["xlsx"])]
        target = Path(str(body.get("directory") or self.config.home / "exports")).expanduser()
        pack_name = str(body.get("pack") or self.pack_name)

        def work(job: Job) -> dict[str, Any]:
            from bmail.outputs.ics import export_ics
            from bmail.outputs.tabular import export_csv, export_xlsx

            pack = self.pack(pack_name)
            conn = self.read()
            try:
                target.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M")
                written: list[str] = []
                for index, kind in enumerate(kinds, start=1):
                    job.raise_if_cancelled()
                    job.update(step=f"writing {kind}", progress=index / (len(kinds) + 1))
                    if kind == "xlsx":
                        written.append(str(export_xlsx(conn, pack, target / f"bmail-{stamp}.xlsx")))
                    elif kind == "ics":
                        written.append(str(export_ics(conn, pack, target / f"bmail-{stamp}.ics")))
                    elif kind == "csv":
                        written.extend(
                            str(p) for p in export_csv(conn, pack, target / f"csv-{stamp}")
                        )
                    else:
                        raise ApiError(f"unknown export kind '{kind}'")
                return {"files": written, "directory": str(target)}
            finally:
                conn.close()

        return self.jobs.submit("export", work)

    # -- accounts and first run ------------------------------------------------------ #

    def setup_state(self, messages: int | None = None) -> dict[str, Any]:
        """First run means nothing has been read and nothing connected yet, and
        the person has not finished (or skipped) the welcome."""
        if messages is None:
            conn = self.read()
            try:
                messages = repo.count_messages(conn)["total"]
            finally:
                conn.close()
        done = self._meta(SETUP_DONE_KEY) == "1"
        return {
            "done": done,
            "first_run": not done and not messages and not self.config.accounts,
            "pack": self.pack_name,
            # Chosen on an earlier start: the welcome resumes after that step.
            "pack_chosen": self._meta(PACK_KEY) is not None,
        }

    def prefs(self) -> dict[str, str]:
        try:
            stored = json.loads(self._meta(PREFS_KEY) or "{}")
        except ValueError:
            return {}
        return {k: str(v) for k, v in stored.items() if k in PREFS and str(v) in PREFS[k]} \
            if isinstance(stored, dict) else {}

    def set_prefs(self, body: dict[str, Any]) -> dict[str, str]:
        prefs = self.prefs()
        for key, value in body.items():
            if key not in PREFS:
                continue
            if str(value) not in PREFS[key]:
                raise ApiError(f"{key} cannot be {value!r}")
            prefs[key] = str(value)
        self._set_meta(PREFS_KEY, json.dumps(prefs, sort_keys=True))
        return prefs

    def setup(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("pack"):
            name = str(body["pack"])
            if name not in available_packs(self.config.pack_dirs):
                raise ApiError(f"unknown pack '{name}'")
            self._set_meta(PACK_KEY, name)
            self.pack_name = name
        if body.get("done"):
            self._set_meta(SETUP_DONE_KEY, "1")
        return self.setup_state()

    def list_accounts(self) -> dict[str, Any]:
        return {
            "accounts": [mailboxes.public(entry) for entry in self.config.accounts],
            "presets": mailboxes.presets(),
            "since_choices": list(mailboxes.SINCE_CHOICES),
            "default_since": mailboxes.DEFAULT_SINCE,
            "keychain": keystore.keychain_status(),
        }

    def check_account(self, body: dict[str, Any]) -> dict[str, Any]:
        # Inline rather than a job: it is one sign-in with a 20-second timeout,
        # and the person is waiting on the answer with the form still open. A
        # job would queue it behind whatever long import is already running.
        return mailboxes.check(body)

    def add_account(self, body: dict[str, Any]) -> dict[str, Any]:
        added = mailboxes.add(self.config, body)
        # A connected mailbox is a finished setup, whether or not the person
        # waits for the welcome to end: closing the app now must not start it
        # all over again.
        self._set_meta(SETUP_DONE_KEY, "1")
        self.syncer.accounts_changed()
        # The first import starts straight away, as an ordinary check of this
        # account: fetch, then sort, on one progress bar.
        job = self.start_ingest({"account": added["account"]["name"]})
        return {**added, "job": job.as_dict()}

    def remove_account(self, name: str) -> dict[str, Any]:
        mailboxes.remove(self.config, name)
        self.syncer.accounts_changed()
        return {"ok": True, "removed": name}

    def start_samples(self, body: dict[str, Any]) -> Job:
        """Read a generated mailbox, for trying Bmail before connecting a real one."""
        from bmail.samples import write_mbox

        directory = self.config.home / "samples"
        self._set_meta(SETUP_DONE_KEY, "1")

        def work(job: Job) -> dict[str, Any]:
            job.update(step="writing sample mail", progress=0.01)
            mbox, _labels = write_mbox(directory)
            return self.sync(job, [{"name": "sample", "provider": "file", "path": str(mbox)}],
                             use_llm=not bool(body.get("no_llm")))

        return self.jobs.submit("ingest", work)

    def sync_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        if "interval" in body:
            self.syncer.set_interval(body["interval"])
        return self.syncer.status()

    def shutdown(self) -> None:
        self.syncer.stop()
        self.jobs.shutdown()


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values and values[0] != "" else None


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _one(query, key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _bool(query: dict[str, list[str]], key: str) -> bool | None:
    raw = _one(query, key)
    if raw is None:
        return None
    return raw.lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    server_version = "Bmail"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: BmailApp

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        return

    def handle_one_request(self) -> None:
        # A client that hangs up mid-request — a closed tab, a reloaded page — is
        # normal, not an error worth printing a stack trace to someone's terminal.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True



    # -- responses -------------------------------------------------------------- #

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, default=_json_default, ensure_ascii=False).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The UI loads nothing from anywhere else, so say so.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- guards -------------------------------------------------------------------- #

    def _host_is_local(self) -> bool:
        """Reject a request whose Host is not loopback.

        A local server is reachable from any page the browser loads, and DNS
        rebinding turns that into "any website can read your mail". Pinning Host
        is the cheap, complete defence.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in {"127.0.0.1", "localhost", "::1"} or host == ""

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # not a browser-initiated cross-site request
        parsed = urlparse(origin)
        return (parsed.hostname or "") in LOCAL_HOSTS

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], self.app.token)
        supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.app.token)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("request body too large", 413)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(f"invalid JSON body: {exc}") from exc

    # -- verbs ------------------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._host_is_local():
            self._send(403, {"error": "this server only answers to 127.0.0.1"})
            return
        if not self._origin_ok():
            self._send(403, {"error": "cross-origin requests are not accepted"})
            return
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    self._send(405, {"error": "method not allowed"})
                else:
                    self._serve_static(path)
                return
            if path != "/api/health" and not self._authorised():
                self._send(401, {"error": "missing or invalid token"})
                return
            query = parse_qs(parsed.query)
            payload = (
                self._route_get(path, query)
                if method == "GET"
                else self._route_post(path, self._body())
            )
            self._send(200, payload)
        except ApiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except BmailError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # never leak a stack trace to the page
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- routing ------------------------------------------------------------------------ #

    def _route_get(self, path: str, query: dict[str, list[str]]) -> Any:
        app = self.app
        if path == "/api/health":
            return {"status": "ok", "pack": app.pack_name}
        if path == "/api/state":
            return app.state(query)
        if path == "/api/brief":
            return app.brief(query)
        if path == "/api/cases":
            return app.cases(query)
        if path.startswith("/api/cases/"):
            return app.case(_case_id(path), query)
        if path == "/api/messages":
            return app.messages(query)
        if path.startswith("/api/messages/"):
            return app.message(_tail(path, "/api/messages/"), query)
        if path == "/api/stats":
            return app.stats(query)
        if path == "/api/sync":
            return app.syncer.status()
        if path == "/api/accounts":
            return app.list_accounts()
        if path == "/api/setup":
            return app.setup_state()
        if path == "/api/jobs":
            return {"jobs": [job.as_dict() for job in app.jobs.list()]}
        if path.startswith("/api/jobs/"):
            job = app.jobs.get(_tail(path, "/api/jobs/"))
            if job is None:
                raise ApiError("no such job", 404)
            return job.as_dict()
        raise ApiError(f"no route {path}", 404)

    def _route_post(self, path: str, body: dict[str, Any]) -> Any:
        app = self.app
        if path == "/api/triage":
            return app.start_triage(body).as_dict()
        if path == "/api/ingest":
            return app.start_ingest(body).as_dict()
        if path == "/api/export":
            return app.start_export(body).as_dict()
        if path == "/api/sync":
            return app.sync_settings(body)
        if path == "/api/setup":
            return app.setup(body)
        if path == "/api/prefs":
            return app.set_prefs(body)
        if path == "/api/samples":
            return app.start_samples(body).as_dict()
        if path == "/api/accounts/check":
            return app.check_account(body)
        if path == "/api/accounts":
            return app.add_account(body)
        if path.startswith("/api/accounts/") and path.endswith("/remove"):
            return app.remove_account(_tail(path[: -len("/remove")], "/api/accounts/"))
        if path.startswith("/api/correct/"):
            return app.correct(_tail(path, "/api/correct/"), body)
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path[len("/api/jobs/") : -len("/cancel")]
            return {"cancelled": app.jobs.cancel(job_id)}
        raise ApiError(f"no route {path}", 404)

    # -- static ---------------------------------------------------------------------------- #

    def _serve_static(self, path: str) -> None:
        root = static_dir()
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (root / relative).resolve()
        try:
            target.relative_to(root.resolve())  # no traversal outside the asset directory
        except ValueError:
            self._send(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send(404, {"error": "not found"}, "application/json")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith(("javascript", "json")):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)


def _tail(path: str, prefix: str) -> str:
    from urllib.parse import unquote

    value = unquote(path[len(prefix) :]).strip("/")
    if not value:
        raise ApiError("missing identifier", 404)
    return value


def _case_id(path: str) -> int:
    try:
        return int(_tail(path, "/api/cases/"))
    except ValueError as exc:
        raise ApiError("case id must be a number") from exc


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _watch_parent(
    server: ThreadingHTTPServer, interval: float = 2.0, *, parent_pid: int | None = None
) -> None:
    """Shut down when the process that started this one goes away.

    The desktop shell kills the daemon when its window closes, but that only
    covers a clean exit. If the shell is killed outright the daemon would
    otherwise survive it — leaving a server that can read someone's mail running
    on their machine with no window to show for it.

    The shell names itself in BMAIL_PARENT_PID, and that process is watched
    directly. Watching our own parent is only the fallback: in the installed
    app the engine is a PyInstaller program, whose Python runs *under* a small
    unpacking process, and that unpacker — the only parent Python can see — does
    not exit when the window does. On Windows an orphan is not even re-parented,
    so a changed parent never shows at all.
    """
    if parent_pid:
        def gone() -> bool:
            return not _alive(parent_pid)
    else:
        original = os.getppid()

        def gone() -> bool:
            return os.getppid() != original

    def watch() -> None:
        while True:
            time.sleep(interval)
            if gone():
                server.shutdown()
                return

    threading.Thread(target=watch, name="bmail-parent-watch", daemon=True).start()


def _alive(pid: int) -> bool:
    """Whether a process exists, without touching it.

    Not os.kill(pid, 0) on Windows: there, any signal other than the two
    console ones terminates the process — the probe would kill what it checks.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import ctypes

        synchronize, wait_timeout = 0x0010_0000, 0x0000_0102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True           # it exists; it just is not ours to signal
    return True


def _parent_pid_from_env() -> int | None:
    try:
        return int(os.environ.get("BMAIL_PARENT_PID", "")) or None
    except ValueError:
        return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second program bind a port that is already
    # in use, so two engines could answer on one address. Elsewhere it only
    # skips the wait after a restart.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Same reasoning as the handler: a dropped connection is not news.
        import sys

        kind = sys.exc_info()[0]
        if kind is not None and issubclass(kind, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def build_server(
    config: Config, *, host: str = "127.0.0.1", port: int | None = 0, pack: str | None = None
) -> tuple[_Server, BmailApp]:
    """`port=None` asks for `DEFAULT_PORT` and takes any free port if it is
    taken; `0` always takes any free port."""
    if host not in LOCAL_HOSTS:
        raise BmailError(
            f"refusing to bind {host}: this server exposes mail content and is loopback-only"
        )
    app = BmailApp(config, pack=pack)
    handler = type("_BoundHandler", (_Handler,), {"app": app})
    if port is None:
        try:
            return _Server((host, DEFAULT_PORT), handler), app
        except OSError:
            port = 0
    return _Server((host, port), handler), app


def serve(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    pack: str | None = None,
    open_browser: bool = False,
    machine_readable: bool = False,
    exit_with_parent: bool = False,
) -> int:
    server, app = build_server(config, host=host, port=port, pack=pack)
    if exit_with_parent:
        _watch_parent(server, parent_pid=_parent_pid_from_env())
    actual = server.server_address[1]
    url = f"http://{host}:{actual}/?token={app.token}"

    if machine_readable:
        # One JSON line for a supervising process (the desktop shell) to parse.
        # Flushed explicitly: stdout is block-buffered when it is a pipe, and a
        # parent waiting on this line would otherwise wait for ever.
        print(json.dumps({"url": url, "port": actual, "token": app.token}), flush=True)
    else:
        print(f"Bmail is running at  http://{host}:{actual}", flush=True)
        print(f"token: {app.token}", flush=True)
        if open_browser:
            print("opening your browser…", flush=True)
        else:
            print(f"open: {url}", flush=True)
        print("ctrl-c to stop", flush=True)
    if open_browser:
        webbrowser.open(url)
    app.syncer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
        app.shutdown()
    return 0
