"""Local-file sources: mbox, Maildir and loose .eml files.

This is the connector to start with. A Gmail Takeout export or an `.mbox` from
any client gives you the whole corpus with no OAuth, no rate limits and no
credentials — which is exactly what you want while you are still learning what
your own mail looks like.
"""

from __future__ import annotations

import mailbox
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sift.errors import IngestError
from sift.ingest.raw import parse_message
from sift.models import Message


class FileSource:
    provider = "file"

    def __init__(
        self,
        path: str | Path,
        *,
        name: str = "local",
        own_addresses: set[str] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.name = name
        self.own_addresses = own_addresses or set()
        if not self.path.exists():
            raise IngestError(f"no such path: {self.path}")

    # -- kind detection ----------------------------------------------------- #

    def _kind(self) -> str:
        if self.path.is_dir():
            if (self.path / "cur").is_dir() and (self.path / "new").is_dir():
                return "maildir"
            return "eml-dir"
        if self.path.suffix.lower() == ".eml":
            return "eml"
        return "mbox"

    # -- fetch -------------------------------------------------------------- #

    def fetch(
        self, cursor: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> Iterator[Message]:
        kind = self._kind()
        count = 0
        for raw, uid, folder in self._iter_raw(kind):
            if limit is not None and count >= limit:
                return
            try:
                yield parse_message(
                    raw,
                    provider=self.provider,
                    account=self.name,
                    uid=uid,
                    folder=folder,
                    own_addresses=self.own_addresses,
                )
            except Exception as exc:  # a single unparseable mail must not stop a backfill
                raise IngestError(f"failed to parse {uid or self.path}: {exc}") from exc
            count += 1

    def _iter_raw(self, kind: str) -> Iterator[tuple[bytes, str | None, str | None]]:
        if kind == "eml":
            yield self.path.read_bytes(), self.path.name, self.path.parent.name
        elif kind == "eml-dir":
            for file in sorted(self.path.rglob("*.eml")):
                yield file.read_bytes(), file.name, file.parent.name
        elif kind == "maildir":
            box = mailbox.Maildir(str(self.path), factory=None)
            try:
                for key in box.iterkeys():
                    try:
                        yield box.get_bytes(key), key, "maildir"
                    except KeyError:
                        continue
            finally:
                box.close()
        else:
            box = mailbox.mbox(str(self.path), factory=None)
            try:
                for key in box.iterkeys():
                    try:
                        yield box.get_bytes(key), str(key), self.path.stem
                    except KeyError:
                        continue
            finally:
                box.close()

    def cursor(self) -> dict[str, Any]:
        # Files are re-read in full; idempotency comes from Message-ID, not a cursor.
        return {"kind": self._kind(), "path": str(self.path)}
