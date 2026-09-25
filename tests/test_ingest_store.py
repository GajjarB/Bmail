from datetime import datetime, timezone

from bmail.ingest import FileSource, ingest
from bmail.ingest.raw import parse_message, synth_message_id
from bmail.models import Direction
from bmail.samples import write_mbox
from bmail.store import repo
from tests.conftest import make_raw


def test_parse_headers_and_threading():
    raw = make_raw(
        subject="Re: Ihre Bewerbung",
        extra={"References": "<a@x> <b@x>", "In-Reply-To": "<b@x>", "Precedence": "bulk"},
    )
    message = parse_message(raw)
    assert message.subject == "Re: Ihre Bewerbung"
    assert message.references == ["<a@x>", "<b@x>"]
    assert message.thread_id == "<a@x>"  # root of the chain, not the subject
    assert message.precedence == "bulk"
    assert message.date.tzinfo is not None


def test_encoded_words_are_decoded():
    raw = make_raw(subject="=?utf-8?B?SWhyZSBCZXdlcmJ1bmc=?=")
    assert parse_message(raw).subject == "Ihre Bewerbung"


def test_html_only_message_becomes_text():
    message = parse_message(make_raw(html="<p>Hallo <b>Welt</b></p>"))
    assert message.has_html
    assert "Hallo Welt" in message.body_text


def test_direction_uses_own_addresses():
    raw = make_raw(sender="you@example.com", to="other@example.com")
    assert parse_message(raw, own_addresses={"you@example.com"}).direction is Direction.outbound
    assert parse_message(raw).direction is Direction.inbound


def test_missing_message_id_is_deterministic():
    raw = make_raw(message_id="").replace(b"Message-ID: \r\n", b"")
    first, second = parse_message(raw), parse_message(raw)
    assert first.message_id == second.message_id
    assert first.message_id.startswith("<bmail-")


def test_synth_id_differs_on_different_content():
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert synth_message_id("a@b", when, "s", "one") != synth_message_id("a@b", when, "s", "two")


def test_ingest_is_idempotent(conn, tmp_path):
    mbox, _labels = write_mbox(tmp_path, applications=3, noise=4)
    source = FileSource(mbox, name="demo", own_addresses={"you@example.com"})
    first = ingest(conn, source)
    before = repo.count_messages(conn)["total"]
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    assert first.fetched > 0
    assert repo.count_messages(conn)["total"] == before  # re-ingest adds nothing


def test_contact_graph_counts_both_directions(conn):
    inbound = parse_message(make_raw(sender="hr@acme.de", message_id="<1@x>"))
    outbound = parse_message(
        make_raw(sender="you@example.com", to="hr@acme.de", message_id="<2@x>"),
        own_addresses={"you@example.com"},
    )
    repo.bulk_upsert(conn, [inbound, outbound])
    stats = repo.contact_stats(conn, "hr@acme.de", "acme.de")
    assert stats["received_from_address"] == 1
    assert stats["sent_to_address"] == 1


def test_full_text_search_finds_body_terms(conn):
    message = parse_message(make_raw(body="Bitte bearbeiten Sie den Online-Test."))
    repo.bulk_upsert(conn, [message])
    assert repo.search(conn, "Online-Test")


def test_search_survives_malformed_query(conn):
    repo.bulk_upsert(conn, [parse_message(make_raw())])
    assert repo.search(conn, 'unbalanced "quote AND') == []


def test_reopening_the_store_keeps_data(tmp_path):
    from bmail.store.db import connect

    path = tmp_path / "s.sqlite3"
    with connect(path) as _c:
        pass
    first = connect(path)
    repo.bulk_upsert(first, [parse_message(make_raw())])
    first.close()
    second = connect(path)
    assert repo.count_messages(second)["total"] == 1
    second.close()


def test_the_sample_corpus_is_reproducible(tmp_path):
    """The generator documents itself as seeded, and the evaluation split is
    keyed on message ids — so two runs of the same seed must be identical."""
    import hashlib
    from datetime import datetime, timezone

    # Pinned: the corpus is dated relative to `end`, and reading the clock twice
    # made this fail whenever the two runs straddled a second boundary.
    end = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    def digest(directory, seed):
        mbox, labels = write_mbox(directory, seed=seed, applications=5, noise=5, end=end)
        return (
            hashlib.sha256(mbox.read_bytes()).hexdigest(),
            hashlib.sha256(labels.read_bytes()).hexdigest(),
        )

    assert digest(tmp_path / "a", 7) == digest(tmp_path / "b", 7)
    assert digest(tmp_path / "c", 99) != digest(tmp_path / "d", 7)


def test_sample_message_ids_are_unique(tmp_path):
    import json

    _mbox, labels = write_mbox(tmp_path, applications=12, noise=20)
    ids = [json.loads(line)["message_id"] for line in labels.read_text().splitlines()]
    assert len(ids) == len(set(ids))


def test_the_owner_is_named_as_their_own_sent_mail_names_them(conn):
    own = {"you@example.com"}
    sent = [parse_message(make_raw(sender="you@example.com", sender_name="Alex Demo",
                                   message_id=f"<sent{n}@x>"), own_addresses=own) for n in range(2)]
    received = parse_message(make_raw(sender="hr@acme.example", sender_name="Acme HR",
                                      message_id="<in@x>"), own_addresses=own)
    repo.bulk_upsert(conn, [*sent, received])
    assert repo.owner_name(conn) == "Alex Demo"


def test_without_sent_mail_there_is_no_name_to_guess(conn):
    repo.bulk_upsert(conn, [parse_message(make_raw(sender="hr@acme.example", sender_name="Acme HR"))])
    assert repo.owner_name(conn) == ""
