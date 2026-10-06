"""A browser that hangs up mid-response must not print a stack trace."""

from __future__ import annotations

import io

import pytest

from bmail.api import _Handler


class _AbortingWriter(io.RawIOBase):
    def __init__(self, exc: type[OSError]) -> None:
        self.exc = exc

    def writable(self) -> bool:
        return True

    def write(self, _b: bytes) -> int:  # type: ignore[override]
        raise self.exc(10053, "connection aborted by the host")


@pytest.mark.parametrize(
    "exc", [ConnectionAbortedError, ConnectionResetError, BrokenPipeError]
)
def test_client_disconnect_is_swallowed(exc: type[OSError]) -> None:
    handler = _Handler.__new__(_Handler)
    handler.rfile = io.BytesIO(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    handler.wfile = _AbortingWriter(exc)
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    handler.close_connection = False

    handler.handle_one_request()  # must not raise

    assert handler.close_connection is True
