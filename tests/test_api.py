"""The local API. The security properties are the point: loopback only, token
required, read-only."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from sift.api import _Handler
from sift.config import Config
from sift.errors import SiftError
from sift.ingest import FileSource, ingest
from sift.samples import write_mbox
from sift.store import repo


@pytest.fixture()
def server(tmp_path, conn, pack):
    mbox, _ = write_mbox(tmp_path, applications=4, noise=4)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    conn.commit()

    config = Config(home=tmp_path, db_path=tmp_path / "test.sqlite3")
    handler = type(
        "_T", (_Handler,), {"config": config, "token": "test-token", "pack_name": pack.name}
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get(base: str, path: str, token: str | None = "test-token"):
    request = urllib.request.Request(base + path)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def test_health_needs_no_token(server):
    status, payload = _get(server, "/health", token=None)
    assert status == 200 and payload["status"] == "ok"


def test_everything_else_needs_the_token(server):
    for path in ("/brief", "/cases", "/stats", "/messages"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(server, path, token=None)
        assert excinfo.value.code == 401


def test_a_wrong_token_is_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/brief", token="not-the-token")
    assert excinfo.value.code == 401


def test_read_endpoints(server):
    assert _get(server, "/brief?days=400")[1]["pack"]
    assert isinstance(_get(server, "/cases")[1], list)
    assert _get(server, "/stats")[1]["messages"]["total"] > 0
    assert _get(server, "/messages?limit=3")[1].__len__() <= 3
    assert "job-seeker" in _get(server, "/packs")[1]


def test_unknown_routes_are_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/definitely-not-a-route")
    assert excinfo.value.code == 404


def test_serve_refuses_a_non_loopback_bind(tmp_path):
    from sift.api import serve

    with pytest.raises(SiftError, match="loopback"):
        serve(Config(home=tmp_path), host="0.0.0.0")


def test_the_api_cannot_write(server, conn, pack):
    before = repo.count_messages(conn)["total"]
    _get(server, "/brief?days=400")
    request = urllib.request.Request(server + "/cases", data=b"{}", method="POST")
    request.add_header("Authorization", "Bearer test-token")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code in (405, 501)  # no POST handler exists at all
    assert repo.count_messages(conn)["total"] == before
