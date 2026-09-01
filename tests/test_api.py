"""The local application server.

The security properties are the point — loopback-only, token-gated, no
cross-origin writes, no path traversal — and they are tested first because they
are what makes running a mail-reading server on a laptop defensible at all.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from sift.api import build_server
from sift.config import Config
from sift.errors import SiftError
from sift.ingest import FileSource, ingest
from sift.samples import write_mbox
from sift.store import repo
from sift.store.db import connect


@pytest.fixture()
def live(tmp_path):
    """A running server over a small, already-triaged store."""
    from sift.classify.stage2 import Classifier
    from sift.packs import load_pack
    from sift.pipeline import Pipeline

    home = tmp_path / "home"
    home.mkdir()
    config = Config(home=home, db_path=home / "sift.sqlite3")
    mbox, labels = write_mbox(tmp_path, applications=6, noise=8)

    conn = connect(config.database)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    pack = load_pack("job-seeker")
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(
        conn, repo.iter_messages(conn)
    )
    conn.close()

    server, app = build_server(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, app, config, labels
    server.shutdown()
    server.server_close()
    app.shutdown()
    thread.join(timeout=5)


def request(base, path, *, token=None, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw and raw[:1] in b"{[" else raw)


def get(base, app, path, **kwargs):
    return request(base, path, token=app.token, **kwargs)[1]


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


def test_api_requires_a_token(live):
    base, _app, _config, _ = live
    for path in ("/api/state", "/api/brief", "/api/messages"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            request(base, path)
        assert excinfo.value.code == 401


def test_a_wrong_token_is_rejected(live):
    base, _app, _config, _ = live
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        request(base, "/api/state", token="nope")
    assert excinfo.value.code == 401


def test_health_needs_no_token(live):
    base, _app, _config, _ = live
    assert request(base, "/api/health")[1]["status"] == "ok"


def test_a_rebound_host_header_is_refused(live):
    """DNS rebinding is how a web page reaches a localhost server."""
    base, app, _config, _ = live
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        request(base, "/api/state", token=app.token, headers={"Host": "evil.example.com"})
    assert excinfo.value.code == 403


def test_cross_origin_requests_are_refused(live):
    base, app, _config, _ = live
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        request(base, "/api/state", token=app.token,
                headers={"Origin": "https://evil.example.com"})
    assert excinfo.value.code == 403


def test_a_local_origin_is_accepted(live):
    base, app, _config, _ = live
    port = base.rsplit(":", 1)[1]
    status, _ = request(base, "/api/state", token=app.token,
                        headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200


def test_static_files_cannot_escape_their_directory(live):
    base, _app, _config, _ = live
    # urllib normalises "..", so speak HTTP directly.
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(base)
    sock = socket.create_connection((parts.hostname, parts.port), timeout=10)
    sock.sendall(b"GET /../../../etc/passwd HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    head = sock.recv(64).decode(errors="replace")
    sock.close()
    assert " 200 " not in head


def test_binding_a_public_interface_is_refused(tmp_path):
    with pytest.raises(SiftError, match="loopback"):
        build_server(Config(home=tmp_path), host="0.0.0.0")


def test_responses_carry_a_strict_csp(live):
    base, _app, _config, _ = live
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        policy = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def test_state_bootstraps_the_ui(live):
    base, app, _config, _ = live
    state = get(base, app, "/api/state")
    assert state["pack"] == "job-seeker"
    assert state["messages"]["total"] > 0
    assert state["verdicts"]["total"] > 0
    assert any(c["name"] == "interview_invite" for c in state["categories"])
    assert "available" in state["llm"]
    assert "password" not in json.dumps(state["configured_accounts"])


def test_messages_filter_and_page(live):
    base, app, _config, _ = live
    everything = get(base, app, "/api/messages?limit=5")
    assert everything["total"] > 5 and len(everything["messages"]) == 5

    second = get(base, app, "/api/messages?limit=5&offset=5")
    assert {m["message_id"] for m in second["messages"]}.isdisjoint(
        {m["message_id"] for m in everything["messages"]}
    )

    filtered = get(base, app, "/api/messages?category=rejection")
    assert all(m["category"] == "rejection" for m in filtered["messages"])

    searched = get(base, app, "/api/messages?q=Bewerbung")
    assert searched["total"] >= 1


def test_a_malformed_search_returns_empty_not_an_error(live):
    base, app, _config, _ = live
    assert get(base, app, "/api/messages?q=%22unbalanced%20AND")["total"] == 0


def test_message_detail_includes_body_and_verdict(live):
    base, app, _config, _ = live
    first = get(base, app, "/api/messages?limit=1")["messages"][0]
    detail = get(base, app, f"/api/messages/{first['message_id']}")
    assert detail["message"]["subject"] == first["subject"]
    assert detail["verdict"]["category"]
    assert "body_text" in detail["message"]


def test_cases_and_one_case_timeline(live):
    base, app, _config, _ = live
    cases = get(base, app, "/api/cases")
    assert cases["cases"] and cases["states"]
    case_id = cases["cases"][0]["id"]
    detail = get(base, app, f"/api/cases/{case_id}")
    assert detail["case"]["id"] == case_id
    assert isinstance(detail["timeline"], list)


def test_unknown_routes_and_ids(live):
    base, app, _config, _ = live
    for path in ("/api/nope", "/api/cases/999999", "/api/messages/%3Cnot@here%3E"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            request(base, path, token=app.token)
        assert excinfo.value.code == 404


def test_a_bad_case_id_is_a_clean_error(live):
    base, app, _config, _ = live
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        request(base, "/api/cases/abc", token=app.token)
    assert excinfo.value.code == 400


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def test_a_correction_updates_the_verdict_and_trains(live):
    base, app, config, _ = live
    target = get(base, app, "/api/messages?limit=1")["messages"][0]["message_id"]
    result = request(base, f"/api/correct/{target}", token=app.token, method="POST",
                     body={"category": "personal", "action_required": True})[1]
    assert result["ok"]

    conn = connect(config.database)
    try:
        verdict = repo.get_verdict(conn, target, "job-seeker")
        assert verdict.category == "personal" and verdict.stage.value == "manual"
        rows = conn.execute(
            "SELECT category FROM training WHERE message_id = ? AND source = 'user'", (target,)
        ).fetchall()
        assert [r["category"] for r in rows] == ["personal"]
    finally:
        conn.close()


def test_a_correction_to_an_unknown_category_is_refused(live):
    base, app, _config, _ = live
    target = get(base, app, "/api/messages?limit=1")["messages"][0]["message_id"]
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        request(base, f"/api/correct/{target}", token=app.token, method="POST",
                body={"category": "not-a-category"})
    assert excinfo.value.code == 400


def test_a_malformed_body_is_a_clean_error(live):
    base, app, _config, _ = live
    req = urllib.request.Request(base + "/api/triage", data=b"{not json", method="POST")
    req.add_header("Authorization", f"Bearer {app.token}")
    req.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=10)
    assert excinfo.value.code == 400


def test_static_files_reject_writes(live):
    base, app, _config, _ = live
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        request(base, "/", token=app.token, method="POST", body={})
    assert excinfo.value.code == 405


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


def _await_job(base, app, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get(base, app, f"/api/jobs/{job_id}")
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish in time")


def test_triage_runs_as_a_job(live):
    base, app, _config, _ = live
    started = request(base, "/api/triage", token=app.token, method="POST",
                      body={"all": True, "no_llm": True})[1]
    assert started["status"] in ("queued", "running")
    job = _await_job(base, app, started["id"])
    assert job["status"] == "done", job.get("error")
    assert job["result"]["processed"] > 0
    assert job["progress"] == 1.0


def test_ingest_runs_as_a_job(live, tmp_path):
    base, app, _config, _ = live
    mbox, _ = write_mbox(tmp_path / "more", applications=2, noise=2, seed=99)
    started = request(base, "/api/ingest", token=app.token, method="POST",
                      body={"path": str(mbox), "account": "second"})[1]
    job = _await_job(base, app, started["id"])
    assert job["status"] == "done", job.get("error")
    assert sum(job["result"]["fetched"].values()) > 0


def test_export_runs_as_a_job(live, tmp_path):
    base, app, _config, _ = live
    started = request(base, "/api/export", token=app.token, method="POST",
                      body={"kinds": ["xlsx", "ics"], "directory": str(tmp_path / "out")})[1]
    job = _await_job(base, app, started["id"])
    assert job["status"] == "done", job.get("error")
    assert len(job["result"]["files"]) == 2
    assert all(p.endswith((".xlsx", ".ics")) for p in job["result"]["files"])


def test_a_failing_job_reports_instead_of_killing_the_worker(live):
    """Submitting always succeeds; the work itself is what can fail.

    The handler only queues, so a bad request surfaces as a failed job rather
    than an HTTP error — and the single worker has to survive it, or the UI
    silently stops accepting work for the rest of the session.
    """
    base, app, _config, _ = live
    broken = request(base, "/api/export", token=app.token, method="POST",
                     body={"kinds": ["not-a-format"]})[1]
    failed = _await_job(base, app, broken["id"])
    assert failed["status"] == "error"
    assert "not-a-format" in failed["error"]

    started = request(base, "/api/triage", token=app.token, method="POST",
                      body={"no_llm": True})[1]
    assert _await_job(base, app, started["id"])["status"] == "done"


def test_jobs_are_listed(live):
    base, app, _config, _ = live
    started = request(base, "/api/triage", token=app.token, method="POST",
                      body={"no_llm": True})[1]
    _await_job(base, app, started["id"])
    assert any(j["id"] == started["id"] for j in get(base, app, "/api/jobs")["jobs"])


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_the_daemon_exits_when_its_parent_does(tmp_path):
    """A server that can read someone's mail must never outlive its window.

    The desktop shell kills the daemon on a clean close, but that cannot cover
    being killed outright — so the daemon watches its parent as a backstop.
    """
    import subprocess
    import sys
    import textwrap

    home = tmp_path / "home"
    home.mkdir()
    script = textwrap.dedent(f"""
        import subprocess, sys, os
        env = dict(os.environ, SIFT_HOME={str(home)!r})
        child = subprocess.Popen(
            [sys.executable, "-m", "sift", "serve", "--json", "--port", "0",
             "--exit-with-parent"],
            stdout=subprocess.PIPE, env=env, text=True,
        )
        print(child.stdout.readline().strip(), flush=True)
        print(child.pid, flush=True)
    """)
    parent = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=90
    )
    assert parent.returncode == 0, parent.stderr
    announced, child_pid = parent.stdout.strip().splitlines()[:2]
    assert json.loads(announced)["url"].startswith("http://127.0.0.1:")

    # The parent has now exited. The daemon should notice and stop by itself.
    pid = int(child_pid)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return  # gone, as required
        time.sleep(0.5)
    os.kill(pid, 9)
    raise AssertionError("the daemon outlived the process that started it")
