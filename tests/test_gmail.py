"""The Gmail connector, against a fake Gmail API.

Gmail sync is "list everything once, then follow history". Almost everything
that can go wrong is about where the history cursor ends up: behind where it
should be costs a re-read, but ahead of it skips mail for good. The fake models
the parts of the API that decide that — per-message historyIds that move when a
message changes, history records, expiry, deletion — and nothing else.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from bmail.errors import IngestError, SignInRequired
from bmail.ingest import gmail as gmail_module
from bmail.ingest.gmail import GmailSource
from bmail.ingest.graph import GraphAuth

# --------------------------------------------------------------------------- #
# A fake Gmail
# --------------------------------------------------------------------------- #


class HttpError(Exception):
    """Shaped like googleapiclient's: the status lives on `.resp.status`."""

    def __init__(self, status: int) -> None:
        super().__init__(f"<HttpError {status}>")
        self.resp = SimpleNamespace(status=status)


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def raw_mail(subject: str) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<{subject.replace(' ', '-')}@gmail.example>"
    message["Subject"] = subject
    message["From"] = "Recruiter <jobs@acme.example>"
    message["To"] = "me@gmail.example"
    message["Date"] = "Mon, 14 Sep 2026 10:00:00 +0200"
    message.set_content(f"Body of {subject}.")
    return bytes(message)


class FakeGmail:
    def __init__(self) -> None:
        self.history_id = 100
        self.oldest_history = 100       # a startHistoryId below this has expired
        self.store: dict[str, dict] = {}
        self.order: list[str] = []
        self.records: list[tuple[int, str, list[str]]] = []
        self.gets: list[str] = []
        self.history_error: int | None = None
        self.page_size = 2
        self.during_listing = None      # called once, after the first page

    # -- the mailbox --------------------------------------------------------- #

    def add(self, mid: str, labels=("INBOX",), record: bool = True) -> None:
        self.history_id += 1
        self.store[mid] = {"raw": raw_mail(mid), "historyId": self.history_id,
                              "labels": list(labels)}
        self.order.append(mid)
        if record:
            self.records.append((self.history_id, mid, list(labels)))

    def touch(self, mid: str) -> None:
        """A label change: the message's historyId moves, nothing is added."""
        self.history_id += 1
        self.store[mid]["historyId"] = self.history_id

    def delete(self, mid: str) -> None:
        self.history_id += 1
        self.store.pop(mid)
        self.order.remove(mid)

    # -- the API surface ------------------------------------------------------ #

    def users(self):
        return self

    def getProfile(self, userId):
        return _Call(lambda: {"emailAddress": "me@gmail.example",
                              "historyId": str(self.history_id)})

    def messages_list(self, pageToken=None, **_):
        newest_first = list(reversed(self.order))
        start = int(pageToken or 0)
        page = newest_first[start:start + self.page_size]
        more = start + self.page_size < len(newest_first)
        if start == 0 and self.during_listing:
            hook, self.during_listing = self.during_listing, None
            hook(self)
        body = {"messages": [{"id": mid} for mid in page]}
        if more:
            body["nextPageToken"] = str(start + self.page_size)
        return body

    def messages_get(self, id, **_):
        self.gets.append(id)
        if id not in self.store:
            raise HttpError(404)
        m = self.store[id]
        return {"raw": base64.urlsafe_b64encode(m["raw"]).decode(), "threadId": f"t-{id}",
                "historyId": str(m["historyId"])}

    def history_list(self, startHistoryId, pageToken=None, **_):
        if self.history_error:
            raise HttpError(self.history_error)
        if int(startHistoryId) < self.oldest_history:
            raise HttpError(404)
        after = [r for r in self.records if r[0] > int(startHistoryId)]
        start = int(pageToken or 0)
        page = after[start:start + self.page_size]
        body = {
            "history": [{"id": str(h), "messagesAdded": [{"message": {"id": mid,
                        "labelIds": labels}}]} for h, mid, labels in page],
            "historyId": str(self.history_id),
        }
        if start + self.page_size < len(after):
            body["nextPageToken"] = str(start + self.page_size)
        return body

    def messages(self):
        fake = self
        return SimpleNamespace(
            list=lambda **kw: _Call(lambda: fake.messages_list(**kw)),
            get=lambda **kw: _Call(lambda: fake.messages_get(**kw)),
        )

    def history(self):
        fake = self
        return SimpleNamespace(list=lambda **kw: _Call(lambda: fake.history_list(**kw)))


def source(fake: FakeGmail, tmp_path, **kwargs) -> GmailSource:
    src = GmailSource(name="gmail-test", client_secret_file=tmp_path / "client.json",
                      token_file=tmp_path / "token.json", **kwargs)
    src._service = fake
    return src


def subjects(messages) -> list[str]:
    return [m.subject for m in messages]


@pytest.fixture
def fake():
    return FakeGmail()


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def test_the_first_sync_reads_the_mailbox_and_starts_history_from_then(fake, tmp_path):
    for mid in ("one", "two", "three"):
        fake.add(mid)
    src = source(fake, tmp_path)
    assert subjects(src.fetch()) == ["three", "two", "one"]
    assert src.cursor() == {"history_id": "103"}


def test_the_next_sync_reads_only_what_history_added(fake, tmp_path):
    fake.add("one")
    src = source(fake, tmp_path)
    list(src.fetch())
    fake.add("two")
    fake.add("three")
    again = source(fake, tmp_path)
    assert subjects(again.fetch(src.cursor())) == ["two", "three"]
    assert again.cursor() == {"history_id": "103"}


def test_mail_arriving_during_the_first_sync_is_not_skipped(fake, tmp_path):
    """The cursor used to be the highest historyId among the messages fetched.
    But a message's historyId moves whenever it changes — so if mail arrived
    during the listing and an older message was then relabelled, the cursor
    jumped past the new mail and the next sync never saw it. The cursor has to
    be the mailbox's position when the listing began."""
    for mid in ("one", "two", "three"):
        fake.add(mid)

    def meanwhile(api):
        api.add("arrived-during")   # not in this listing
        api.touch("one")            # on the next page, now with a later historyId

    fake.during_listing = meanwhile
    src = source(fake, tmp_path)
    list(src.fetch())
    later = source(fake, tmp_path)
    assert "arrived-during" in subjects(later.fetch(src.cursor()))


def test_drafts_in_history_are_not_treated_as_mail(fake, tmp_path):
    fake.add("one")
    src = source(fake, tmp_path)
    list(src.fetch())
    fake.add("half-written reply", labels=("DRAFT",))
    fake.add("real mail")
    again = source(fake, tmp_path)
    assert subjects(again.fetch(src.cursor())) == ["real mail"]


def test_a_message_deleted_before_it_is_fetched_is_skipped_not_fatal(fake, tmp_path):
    """History still lists a message that was deleted a moment later, and
    fetching it 404s. That used to end the whole sync."""
    fake.add("one")
    src = source(fake, tmp_path)
    list(src.fetch())
    fake.add("gone")
    fake.delete("gone")
    fake.add("kept")
    again = source(fake, tmp_path)
    assert subjects(again.fetch(src.cursor())) == ["kept"]


def test_the_same_message_twice_in_history_is_fetched_once(fake, tmp_path):
    fake.add("one")
    src = source(fake, tmp_path)
    list(src.fetch())
    fake.add("twice")
    fake.records.append((fake.history_id + 1, "twice", ["INBOX"]))
    fake.history_id += 1
    fake.gets.clear()
    again = source(fake, tmp_path)
    assert subjects(again.fetch(src.cursor())) == ["twice"]
    assert fake.gets.count("twice") == 1


def test_an_expired_cursor_says_so(fake, tmp_path):
    fake.add("one")
    fake.oldest_history = 500
    src = source(fake, tmp_path)
    with pytest.raises(IngestError, match="expired"):
        list(src.fetch({"history_id": "101"}))


def test_a_server_error_is_not_reported_as_an_expired_cursor(fake, tmp_path):
    """Every failure used to be reported as an expired cursor, which tells the
    user to throw away their sync state over a passing server error."""
    fake.add("one")
    fake.history_error = 503
    src = source(fake, tmp_path)
    with pytest.raises(IngestError) as caught:
        list(src.fetch({"history_id": "101"}))
    assert "expired" not in str(caught.value)
    assert "503" in str(caught.value)


def test_a_truncated_incremental_sync_does_not_move_the_cursor(fake, tmp_path):
    """Stopping part-way through history and saving a later cursor would skip
    everything not yet read. Keeping the old one only costs a re-read."""
    fake.add("one")
    src = source(fake, tmp_path)
    list(src.fetch())
    before = src.cursor()
    for mid in ("two", "three", "four"):
        fake.add(mid)
    partial = source(fake, tmp_path)
    assert subjects(partial.fetch(before, limit=1)) == ["two"]
    assert partial.cursor() == {} or partial.cursor() == before


# --------------------------------------------------------------------------- #
# Signing in, when nobody is watching
# --------------------------------------------------------------------------- #


class _Creds:
    def __init__(self, *, valid=False, expired=False, refresh_token=None):
        self.valid, self.expired, self.refresh_token = valid, expired, refresh_token
        self.refreshed = False

    def refresh(self, _request):
        self.refreshed = True
        self.valid = True

    def to_json(self):
        return "{}"


def fake_google(monkeypatch, creds: _Creds | None):
    flows: list[str] = []

    class Credentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            return creds

    class InstalledAppFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            flows.append(path)
            return SimpleNamespace(run_local_server=lambda port: _Creds(valid=True))

    monkeypatch.setattr(gmail_module, "_require_libs",
                        lambda: (object, Credentials, InstalledAppFlow,
                                 lambda *a, **k: "service"))
    return flows


def test_an_unattended_gmail_sync_never_opens_a_sign_in_window(monkeypatch, tmp_path):
    flows = fake_google(monkeypatch, creds=None)
    (tmp_path / "client.json").write_text("{}")
    src = GmailSource(name="g", client_secret_file=tmp_path / "client.json",
                      token_file=tmp_path / "token.json", interactive=False)
    with pytest.raises(SignInRequired):
        src.service()
    assert flows == [], "a browser sign-in was started with nobody there to finish it"


def test_an_unattended_gmail_sync_still_refreshes_a_token_silently(monkeypatch, tmp_path):
    creds = _Creds(expired=True, refresh_token="r")
    flows = fake_google(monkeypatch, creds=creds)
    (tmp_path / "token.json").write_text("{}")
    src = GmailSource(name="g", client_secret_file=tmp_path / "client.json",
                      token_file=tmp_path / "token.json", interactive=False)
    assert src.service() == "service"
    assert creds.refreshed and flows == []


def test_an_interactive_gmail_sync_still_offers_sign_in(monkeypatch, tmp_path):
    flows = fake_google(monkeypatch, creds=None)
    (tmp_path / "client.json").write_text("{}")
    src = GmailSource(name="g", client_secret_file=tmp_path / "client.json",
                      token_file=tmp_path / "token.json")
    assert src.service() == "service"
    assert len(flows) == 1


def test_an_unattended_outlook_sync_never_opens_a_sign_in_window(tmp_path):
    prompted: list[str] = []
    app = SimpleNamespace(
        get_accounts=lambda: [],
        acquire_token_silent=lambda *a, **k: None,
        acquire_token_interactive=lambda **k: prompted.append("interactive") or {},
        initiate_device_flow=lambda **k: prompted.append("device") or {},
    )
    auth = GraphAuth("client", token_cache_file=tmp_path / "cache.json",
                     scopes=["Mail.Read"], interactive=False)
    auth._app = app
    with pytest.raises(SignInRequired):
        auth.token()
    assert prompted == []
