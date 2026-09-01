"""The Microsoft Graph connector.

Driven by a fake transport rather than a live tenant: the logic worth testing is
delta paging, cursor safety, throttling and the JSON fallback, and all of it must
behave identically whatever the network does. MSAL is never imported here.
"""

from __future__ import annotations

import json
from email.message import EmailMessage

import pytest

from sift.errors import IngestError
from sift.ingest import build_source, ingest
from sift.ingest.graph import (
    GRAPH,
    DeltaExpired,
    GraphHTTP,
    GraphSource,
)
from sift.models import Direction
from sift.store import repo

# --------------------------------------------------------------------------- #
# A fake Graph
# --------------------------------------------------------------------------- #


def mime(subject: str, sender: str, body: str, when: str = "Mon, 14 Sep 2026 10:00:00 +0200",
         message_id: str = "<m1@outlook.example>") -> bytes:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "you@example.com"
    message["Date"] = when
    message.set_content(body)
    return bytes(message)


class FakeGraph:
    """Routes Graph URLs to canned responses and records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, list] = {}
        self.calls: list[tuple[str, str]] = []
        self.patched: list[dict] = []
        self.slept: list[float] = []

    def route(self, url: str, *responses) -> None:
        """Queue responses for a URL. Each is (status, headers, body-bytes)."""
        self.routes.setdefault(url, []).extend(responses)

    def json_route(self, url: str, *payloads: dict) -> None:
        self.route(url, *[(200, {}, json.dumps(p).encode()) for p in payloads])

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        assert headers["Authorization"].startswith("Bearer "), "every request must be authorised"
        if method == "PATCH":
            self.patched.append(json.loads(body))
            return 200, {}, b"{}"
        queued = self.routes.get(url)
        if not queued:
            return 404, {}, json.dumps({"error": {"code": "itemNotFound"}}).encode()
        return queued.pop(0) if len(queued) > 1 else queued[0]

    def http(self, **kwargs) -> GraphHTTP:
        return GraphHTTP(
            lambda force=False: "token", transport=self, sleep=self.slept.append, **kwargs
        )


@pytest.fixture()
def graph() -> FakeGraph:
    return FakeGraph()


def source(graph: FakeGraph, **kwargs) -> GraphSource:
    return GraphSource(
        name="outlook", http=graph.http(), own_addresses={"you@example.com"}, **kwargs
    )


DELTA = f"{GRAPH}/me/mailFolders/inbox/messages/delta"


def delta_url(page_size: int = 50) -> str:
    from sift.ingest.graph import DELTA_SELECT

    return f"{DELTA}?$select={DELTA_SELECT}&$top={page_size}"


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def test_fetch_parses_mime_through_the_shared_normaliser(graph):
    graph.json_route(
        delta_url(),
        {
            "value": [{"id": "AAA", "conversationId": "conv-1",
                       "internetMessageId": "<m1@outlook.example>"}],
            "@odata.deltaLink": "https://delta/1",
        },
    )
    graph.route(
        f"{GRAPH}/me/messages/AAA/$value",
        (200, {}, mime("Ihre Bewerbung", "HR <hr@acme.de>", "Vielen Dank für Ihre Bewerbung.")),
    )
    messages = list(source(graph).fetch())
    assert len(messages) == 1
    message = messages[0]
    assert message.subject == "Ihre Bewerbung"
    assert message.provider == "graph"
    assert message.uid == "AAA"
    assert message.thread_id == "conv-1"          # Graph's conversation id wins
    assert message.language == "de"               # normalised by the shared pipeline
    assert message.direction is Direction.inbound


def test_delta_paging_follows_next_links(graph):
    graph.json_route(
        delta_url(),
        {"value": [{"id": "A"}], "@odata.nextLink": "https://next/2"},
    )
    graph.json_route(
        "https://next/2",
        {"value": [{"id": "B"}], "@odata.deltaLink": "https://delta/final"},
    )
    for item in ("A", "B"):
        graph.route(f"{GRAPH}/me/messages/{item}/$value",
                    (200, {}, mime(f"Subject {item}", "x@y.de", "body", message_id=f"<{item}@x>")))
    connector = source(graph)
    assert len(list(connector.fetch())) == 2
    assert connector.cursor() == {"delta_link": "https://delta/final", "folder": "inbox"}


def test_removed_items_are_skipped(graph):
    graph.json_route(
        delta_url(),
        {
            "value": [
                {"id": "A"},
                {"id": "B", "@removed": {"reason": "deleted"}},
            ],
            "@odata.deltaLink": "https://delta/1",
        },
    )
    graph.route(f"{GRAPH}/me/messages/A/$value", (200, {}, mime("Kept", "x@y.de", "body")))
    assert len(list(source(graph).fetch())) == 1
    # The removed item was never fetched at all.
    assert ("GET", f"{GRAPH}/me/messages/B/$value") not in graph.calls


def test_a_stored_delta_link_is_resumed(graph):
    graph.json_route("https://delta/stored", {"value": [], "@odata.deltaLink": "https://delta/2"})
    connector = source(graph)
    list(connector.fetch({"delta_link": "https://delta/stored", "folder": "inbox"}))
    assert ("GET", "https://delta/stored") in graph.calls
    assert ("GET", delta_url()) not in graph.calls


def test_a_cursor_from_another_folder_is_ignored(graph):
    graph.json_route(delta_url(), {"value": [], "@odata.deltaLink": "https://delta/2"})
    list(source(graph).fetch({"delta_link": "https://delta/old", "folder": "archive"}))
    assert ("GET", delta_url()) in graph.calls  # a full read of the configured folder


def test_an_expired_delta_link_falls_back_to_a_full_sync(graph):
    graph.route(
        "https://delta/stale",
        (410, {}, json.dumps({"error": {"code": "syncStateNotFound"}}).encode()),
    )
    graph.json_route(delta_url(), {"value": [], "@odata.deltaLink": "https://delta/new"})
    connector = source(graph)
    list(connector.fetch({"delta_link": "https://delta/stale", "folder": "inbox"}))
    assert connector.cursor()["delta_link"] == "https://delta/new"


def test_an_expired_link_mid_run_is_not_swallowed(graph):
    graph.route(
        delta_url(), (410, {}, json.dumps({"error": {"code": "resyncRequired"}}).encode())
    )
    with pytest.raises(DeltaExpired):
        list(source(graph).fetch())


def test_the_whole_mailbox_can_be_read(graph):
    from sift.ingest.graph import DELTA_SELECT

    url = f"{GRAPH}/me/messages/delta?$select={DELTA_SELECT}&$top=50"
    graph.json_route(url, {"value": [], "@odata.deltaLink": "https://delta/1"})
    list(source(graph, folder="").fetch())
    assert ("GET", url) in graph.calls


# --------------------------------------------------------------------------- #
# Cursor safety
# --------------------------------------------------------------------------- #


def test_a_truncated_run_does_not_advance_the_cursor(graph):
    """--limit must never store a delta link: it would skip the unread rest."""
    graph.json_route(
        delta_url(),
        {"value": [{"id": "A"}, {"id": "B"}, {"id": "C"}], "@odata.deltaLink": "https://delta/1"},
    )
    for item in ("A", "B", "C"):
        graph.route(f"{GRAPH}/me/messages/{item}/$value",
                    (200, {}, mime(item, "x@y.de", "body", message_id=f"<{item}@x>")))
    connector = source(graph)
    assert len(list(connector.fetch(limit=2))) == 2
    assert connector.cursor() == {}


def test_a_truncated_resume_keeps_the_previous_cursor(graph):
    stored = {"delta_link": "https://delta/stored", "folder": "inbox"}
    graph.json_route(
        "https://delta/stored",
        {"value": [{"id": "A"}, {"id": "B"}], "@odata.deltaLink": "https://delta/next"},
    )
    for item in ("A", "B"):
        graph.route(f"{GRAPH}/me/messages/{item}/$value",
                    (200, {}, mime(item, "x@y.de", "b", message_id=f"<{item}@x>")))
    connector = source(graph)
    list(connector.fetch(stored, limit=1))
    assert connector.cursor() == stored  # unchanged, so the rest is read next time


# --------------------------------------------------------------------------- #
# JSON fallback
# --------------------------------------------------------------------------- #


def test_items_without_mime_fall_back_to_json(graph):
    graph.json_route(
        delta_url(), {"value": [{"id": "AAA"}], "@odata.deltaLink": "https://delta/1"}
    )
    graph.route(f"{GRAPH}/me/messages/AAA/$value", (404, {}, b"not available"))
    from sift.ingest.graph import FULL_SELECT

    graph.json_route(
        f"{GRAPH}/me/messages/AAA?$select={FULL_SELECT}",
        {
            "id": "AAA",
            "conversationId": "conv-9",
            "internetMessageId": "<json@outlook.example>",
            "subject": "Interview invitation",
            "from": {"emailAddress": {"name": "Sarah Klein", "address": "SARAH@acme.de"}},
            "toRecipients": [{"emailAddress": {"address": "you@example.com"}}],
            "receivedDateTime": "2026-09-14T08:00:00Z",
            "body": {"contentType": "html", "content": "<p>We would like to <b>invite</b> you.</p>"},
            "hasAttachments": True,
            "internetMessageHeaders": [
                {"name": "List-Unsubscribe", "value": "<https://x.example/u>"},
                {"name": "References", "value": "<root@x> <parent@x>"},
                {"name": "X-Irrelevant", "value": "dropped"},
            ],
        },
    )
    graph.json_route(
        f"{GRAPH}/me/messages/AAA/attachments?$select=name,contentType,size",
        {"value": [{"name": "cv.pdf", "contentType": "application/pdf", "size": 1234}]},
    )
    message = list(source(graph).fetch())[0]
    assert message.message_id == "<json@outlook.example>"
    assert message.from_.address == "sarah@acme.de"        # lowercased
    assert message.has_html and "invite" in message.body_text
    assert "<p>" not in message.body_text                  # HTML really was stripped
    assert message.list_unsubscribe == "<https://x.example/u>"
    assert "x-irrelevant" not in message.headers           # only kept headers survive
    assert message.references == ["<root@x>", "<parent@x>"]
    assert message.thread_id == "conv-9"
    assert message.attachments[0].filename == "cv.pdf"
    assert message.date.year == 2026


def test_json_fallback_synthesises_a_missing_message_id(graph):
    payload = {
        "id": "B",
        "subject": "No internet id",
        "from": {"emailAddress": {"address": "x@y.de"}},
        "receivedDateTime": "2026-09-14T08:00:00Z",
        "body": {"contentType": "text", "content": "plain text body"},
    }
    first = GraphSource.build_message(payload)
    second = GraphSource.build_message(payload)
    assert first.message_id.startswith("<sift-")
    assert first.message_id == second.message_id  # deterministic, so re-ingest is idempotent


def test_json_fallback_detects_outbound_mail():
    payload = {
        "id": "C",
        "subject": "Sent",
        "from": {"emailAddress": {"address": "you@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "hr@acme.de"}}],
        "sentDateTime": "2026-09-14T08:00:00Z",
        "body": {"contentType": "text", "content": "my reply"},
    }
    message = GraphSource.build_message(payload, own_addresses={"you@example.com"})
    assert message.direction is Direction.outbound


def test_json_fallback_survives_a_sparse_payload():
    message = GraphSource.build_message({"id": "D"})
    assert message.message_id and message.subject == ""
    assert message.date.tzinfo is not None


# --------------------------------------------------------------------------- #
# Throttling and errors
# --------------------------------------------------------------------------- #


def test_throttling_honours_retry_after(graph):
    graph.route(
        delta_url(),
        (429, {"Retry-After": "7"}, b"{}"),
        (200, {}, json.dumps({"value": [], "@odata.deltaLink": "https://d/1"}).encode()),
    )
    list(source(graph).fetch())
    assert graph.slept == [7.0]  # the header was obeyed, not a guess


def test_server_errors_back_off_then_give_up(graph):
    graph.route(delta_url(), (503, {}, b"{}"))
    with pytest.raises(IngestError):
        list(source(graph).fetch())
    assert len(graph.slept) == 4  # max_retries attempts, then the error surfaces


def test_an_expired_token_is_refreshed_once(graph):
    calls: list[bool] = []

    def token(force_refresh: bool = False) -> str:
        calls.append(force_refresh)
        return "expired" if not force_refresh else "fresh"

    graph.route(
        delta_url(),
        (401, {}, b"{}"),
        (200, {}, json.dumps({"value": [], "@odata.deltaLink": "https://d/1"}).encode()),
    )
    http = GraphHTTP(token, transport=graph, sleep=graph.slept.append)
    list(GraphSource(name="o", http=http, own_addresses={"you@example.com"}).fetch())
    assert True in calls  # a forced refresh happened


def test_a_forbidden_response_explains_the_scope(graph):
    graph.route(
        delta_url(), (403, {}, json.dumps({"error": {"code": "ErrorAccessDenied"}}).encode())
    )
    with pytest.raises(IngestError, match="Mail.Read"):
        list(source(graph).fetch())


# --------------------------------------------------------------------------- #
# Write-back
# --------------------------------------------------------------------------- #


def test_categories_are_not_written_without_allow_modify(graph):
    with pytest.raises(IngestError, match="allow_modify"):
        source(graph).apply_label("AAA", "Sift/Interview")


def test_a_category_is_added_to_the_existing_ones(graph):
    graph.json_route(f"{GRAPH}/me/messages/AAA?$select=categories", {"categories": ["Work"]})
    source(graph, allow_modify=True).apply_label("AAA", "Sift/Interview")
    assert graph.patched == [{"categories": ["Work", "Sift/Interview"]}]


def test_an_existing_category_is_not_written_twice(graph):
    graph.json_route(
        f"{GRAPH}/me/messages/AAA?$select=categories", {"categories": ["Sift/Interview"]}
    )
    source(graph, allow_modify=True).apply_label("AAA", "Sift/Interview")
    assert graph.patched == []


def test_commas_are_stripped_from_category_names(graph):
    """Outlook stores categories comma-delimited; a comma would split the name."""
    graph.json_route(f"{GRAPH}/me/messages/AAA?$select=categories", {"categories": []})
    source(graph, allow_modify=True).apply_label("AAA", "Sift/Offer, signed")
    assert "," not in graph.patched[0]["categories"][0]


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider", ["graph", "outlook", "microsoft", "m365"])
def test_the_registry_builds_a_graph_source(provider):
    built = build_source({"name": "o", "provider": provider, "client_id": "abc"})
    assert isinstance(built, GraphSource)
    assert built.provider == "graph"


def test_read_only_by_default_and_modify_only_on_request():
    from sift.ingest.graph import MODIFY_SCOPE, READ_SCOPE

    assert build_source({"name": "o", "provider": "graph", "client_id": "a"}).scopes == [READ_SCOPE]
    assert build_source(
        {"name": "o", "provider": "graph", "client_id": "a", "allow_modify": True}
    ).scopes == [MODIFY_SCOPE]


def test_a_missing_client_id_is_a_clear_error():
    with pytest.raises(IngestError, match="client_id"):
        build_source({"name": "o", "provider": "graph"})


def test_ingest_stores_graph_messages_and_the_cursor(conn, graph):
    graph.json_route(
        delta_url(), {"value": [{"id": "A", "conversationId": "c1"}],
                      "@odata.deltaLink": "https://delta/1"}
    )
    graph.route(
        f"{GRAPH}/me/messages/A/$value",
        (200, {}, mime("Your application", "hr@acme.de", "Thank you for applying.")),
    )
    result = ingest(conn, source(graph))
    assert result.fetched == 1
    assert repo.count_messages(conn)["total"] == 1
    assert repo.get_cursor(conn, "outlook")["delta_link"] == "https://delta/1"


def test_a_second_run_resumes_from_the_stored_cursor(conn, graph):
    graph.json_route(
        delta_url(), {"value": [], "@odata.deltaLink": "https://delta/1"}
    )
    ingest(conn, source(graph))
    graph.json_route("https://delta/1", {"value": [], "@odata.deltaLink": "https://delta/2"})
    ingest(conn, source(graph))
    assert ("GET", "https://delta/1") in graph.calls
    assert repo.get_cursor(conn, "outlook")["delta_link"] == "https://delta/2"
