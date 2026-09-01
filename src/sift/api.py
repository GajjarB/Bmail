"""A local, read-only HTTP API — the seam a desktop UI would sit behind.

Three rules, and they are the whole security model:

* it binds `127.0.0.1` only, never `0.0.0.0`;
* the port is chosen at random unless you pin one;
* every request must carry a token printed once at startup, so another process
  on the same machine cannot read your mail by guessing the port.

It is read-only on purpose. Ingest and triage are long-running jobs that belong
to the CLI or a scheduler, not to an HTTP handler.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from sift.config import Config
from sift.errors import SiftError
from sift.packs import load_pack
from sift.store import repo
from sift.store.db import connect

LOCAL_ONLY = {"127.0.0.1", "localhost", "::1"}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class _Handler(BaseHTTPRequestHandler):
    server_version = "Sift"
    sys_version = ""
    config: Config
    token: str
    pack_name: str

    # -- plumbing ------------------------------------------------------------- #

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=_json_default, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], self.token)
        query = parse_qs(urlparse(self.path).query)
        supplied = (query.get("token") or [""])[0]
        return secrets.compare_digest(supplied, self.token)

    # -- routing ---------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send(200, {"status": "ok", "pack": self.pack_name})
            return
        if not self._authorised():
            self._send(401, {"error": "missing or invalid token"})
            return
        try:
            self._send(200, self._route(parsed.path, query))
        except KeyError:
            self._send(404, {"error": f"no route {parsed.path}"})
        except SiftError as exc:
            self._send(400, {"error": str(exc)})

    def _route(self, path: str, query: dict[str, list[str]]) -> Any:
        conn = connect(self.config.database, read_only=True)
        try:
            pack = load_pack(
                (query.get("pack") or [self.pack_name])[0], tuple(self.config.pack_dirs)
            )
            limit = int((query.get("limit") or ["50"])[0])
            if path == "/brief":
                from sift.outputs.brief import build_brief

                days = int((query.get("days") or ["1"])[0])
                return build_brief(conn, pack, window_days=days, limit=limit).model_dump(
                    mode="json"
                )
            if path == "/cases":
                return [
                    case.model_dump(mode="json")
                    for case in repo.list_cases(
                        conn, pack.name, state=(query.get("state") or [None])[0]
                    )[:limit]
                ]
            if path.startswith("/cases/"):
                case_id = int(path.rsplit("/", 1)[-1])
                return {
                    "timeline": [dict(row) for row in repo.case_timeline(conn, case_id)],
                }
            if path == "/messages":
                return [
                    {
                        "message_id": message.message_id,
                        "subject": message.subject,
                        "from": message.from_.address,
                        "date": message.date,
                    }
                    for message in repo.iter_messages(conn, limit=limit)
                ]
            if path == "/search":
                return [dict(row) for row in repo.search(conn, (query.get("q") or [""])[0], limit)]
            if path == "/stats":
                return {
                    "messages": repo.count_messages(conn),
                    "verdicts": repo.verdict_stats(conn, pack.name),
                    "cases": len(repo.list_cases(conn, pack.name)),
                }
            if path == "/packs":
                from sift.packs import available_packs

                return sorted(available_packs(self.config.pack_dirs))
            raise KeyError(path)
        finally:
            conn.close()


def serve(config: Config, *, host: str = "127.0.0.1", port: int = 0, pack: str | None = None) -> int:
    if host not in LOCAL_ONLY:
        raise SiftError(
            f"refusing to bind {host}: the API serves mail content and is loopback-only"
        )
    token = secrets.token_urlsafe(24)
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {"config": config, "token": token, "pack_name": pack or config.pack},
    )
    server = ThreadingHTTPServer((host, port), handler)
    actual = server.server_address[1]
    print(f"listening on http://{host}:{actual}")
    print(f"token: {token}")
    print(f"try:   curl -H 'Authorization: Bearer {token}' http://{host}:{actual}/brief")
    print("ctrl-c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
