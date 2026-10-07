from datetime import datetime, timezone

import pytest

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


def test_mail_read_before_a_better_cleaner_is_cleaned_again(tmp_path):
    from bmail.store.db import connect

    path = tmp_path / "store.sqlite3"
    plain = ("sup { font-size: 100% !important; }\n"
             "Your order has shipped.\n"
             "https://track.example/?qs=" + "AbCdEf0123456789" * 6)
    conn = connect(path)
    message = parse_message(make_raw(body=plain))
    repo.bulk_upsert(conn, [message])
    # As an older version of Bmail would have stored it.
    conn.execute("UPDATE bodies SET body_text = ?", (plain,))
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'text_version'")
    conn.commit()
    conn.close()

    conn = connect(path)
    text = conn.execute("SELECT body_text FROM bodies").fetchone()[0]
    assert text == "Your order has shipped.\ntrack.example"
    assert repo.search(conn, "shipped")          # the search index follows
    conn.close()


def test_a_plain_part_full_of_tags_loses_to_the_html_part():
    from email.message import EmailMessage

    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = "News <news@vendor.example>", "you@example.com", "News"
    mail["Message-ID"] = "<tags@vendor.example>"
    mail.set_content('Catch up on the latest.\n<div class="img"><a href="https://v.example/a">'
                     '<img src="https://v.example/h.jpg"></a></div>\n<p>March news</p>')
    mail.add_alternative(
        "<html><body><p>Catch up on the latest from the conference and all its announcements.</p>"
        "<p>March news</p></body></html>", subtype="html")
    message = parse_message(bytes(mail))
    assert message.body_text == (
        "Catch up on the latest from the conference and all its announcements.\n\nMarch news")
    assert "<div" not in message.body_raw_text


def test_cleaning_a_whole_store_again_rebuilds_the_search_index_in_one_pass(tmp_path):
    """The index has no key on the message, so replacing one row reads all of
    it. Done per message that took nine minutes for eight thousand, with the
    store locked; an app closed before the end started over at the next launch."""
    from bmail.store.db import connect

    path = tmp_path / "store.sqlite3"
    conn = connect(path)
    messages = [parse_message(make_raw(
        subject=f"Note {n}", body=f"Catch up.\n<div class=\"x\"><p>Item number{n} arrived</p></div>",
        message_id=f"<note-{n}@example.com>")) for n in range(40)]
    repo.bulk_upsert(conn, messages)
    # As an older version of Bmail would have stored them: tags and all.
    conn.execute("UPDATE bodies SET body_text = body_raw_text")
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'text_version'")
    conn.commit()
    conn.close()

    conn = connect(path)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    assert conn.execute("SELECT count(*) FROM bodies WHERE body_text LIKE '%<div%'").fetchone()[0] == 0
    assert [row["message_id"] for row in repo.search(conn, "number7")] == ["<note-7@example.com>"]
    assert conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 40
    conn.close()

    # And the work is not repeated: the next opening changes nothing.
    conn = connect(path)
    conn.set_trace_callback(statements.append)
    conn.execute("SELECT 1").fetchone()
    conn.close()
    assert not [s for s in statements if s.lstrip().upper().startswith(("UPDATE BODIES", "DELETE"))]


def test_new_mail_is_not_looked_up_in_the_search_index_first(conn):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    message = parse_message(make_raw(message_id="<fresh@example.com>"))
    repo.upsert_message(conn, message)
    assert not [s for s in statements if "DELETE FROM messages_fts" in s]
    # Read a second time, it replaces its own row instead of adding another.
    repo.upsert_message(conn, message)
    assert conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 1


# --- the message as it was laid out ------------------------------------------- #

def _laid_out(**extra):
    from email.message import EmailMessage

    mail = EmailMessage()
    mail["From"], mail["To"] = "Shop <news@shop.example>", "you@example.com"
    mail["Subject"], mail["Message-ID"] = "Laid out", "<laid-out@shop.example>"
    mail.set_content("Your order is on its way.")
    mail.add_alternative(
        "<html><body><h1>Your order is on its way</h1>"
        "<img src='cid:logo' alt='Shop'><img src='https://t.shop.example/o.gif' alt=''>"
        "<script>steal()</script><a href='javascript:steal()' onclick='steal()'>Track</a>"
        "</body></html>", subtype="html")
    mail.get_payload()[1].add_related(b"\x89PNG-bytes", "image", "png", cid="<logo>")
    return parse_message(bytes(mail), **extra)


def test_the_layout_is_kept_beside_the_text(conn):
    from bmail.text import original

    message = _laid_out()
    assert message.body_text == "Your order is on its way."
    repo.bulk_upsert(conn, [message])
    html = repo.get_original(conn, message.message_id)
    assert "<h1>Your order is on its way</h1>" in html
    # The picture the message carried travels with it; attachments are not stored.
    assert "data:image/png;base64," in html and "cid:logo" not in html
    detail = repo.message_detail(conn, message.message_id, "job-seeker")
    assert detail["message"]["original"] == {"remote_pictures": True}
    # Sorting never loads it.
    assert repo.iter_messages(conn)[0].body_html == ""
    assert original.unpack(original.pack(html)) == html


def test_a_plain_message_has_no_layout_to_show(conn):
    message = parse_message(make_raw(body="Just words.", message_id="<plain@example.com>"))
    repo.bulk_upsert(conn, [message])
    assert repo.get_original(conn, message.message_id) is None
    assert repo.message_detail(conn, message.message_id, "job-seeker")["message"]["original"] is None


def test_what_is_served_can_only_be_looked_at():
    from bmail.text import original

    html = _laid_out().body_html
    shown = original.page(html, pictures=False).decode()
    assert "<script" not in shown and "steal()" not in shown
    assert "onclick" not in shown and "javascript:" not in shown
    assert "<h1>Your order is on its way</h1>" in shown
    assert 'data-not-loaded-src="https://t.shop.example/o.gif"' in shown
    assert ' src="https://t.shop.example' not in shown
    assert ' src="https://t.shop.example/o.gif"' in original.page(html, pictures=True).decode()

    closed = original.policy(pictures=False)
    for power in ("allow-scripts", "allow-forms", "allow-popups", "allow-top-navigation"):
        assert power not in closed, power
    assert closed.startswith("sandbox allow-same-origin;")
    assert "default-src 'none'" in closed and "img-src data:;" in closed
    assert "https:" not in closed, "no remote picture unless it was asked for"
    assert "img-src data: https:;" in original.policy(pictures=True)


def test_a_layout_too_large_to_be_a_letter_is_not_kept():
    from bmail.text import original

    assert original.pack("<p>" + "x" * (original.MAX_HTML_BYTES + 1) + "</p>") is None
    assert original.pack("   ") is None


HOSTILE = {
    "a tag hidden in a tag": ("<scr<script>ipt>alert(1)</script>", ("<script",)),
    "a handler without a space": ('<img src="x"onerror=alert(1)>', ("onerror",)),
    "a frame hidden in a frame": ("<ifra<iframe>me src=//evil.example>", ("<iframe",)),
    "a redirect behind a quoted >": (
        '<meta x=">" http-equiv=refresh content="0;url=https://evil.example">', ("<meta", "refresh")),
    "a referrer the message asks for": (
        '<meta name="referrer" content="unsafe-url"><img referrerpolicy="unsafe-url" '
        'src="https://t.example/a.gif">', ("referrer",)),
    "a script address with a tab in it": ('<a href="java\tscript:alert(1)" ping="//t.example">x</a>',
                                          ("script:", "ping")),
    "a link in a drawing": ('<svg><a xlink:href="https://evil.example"><text>hi</text></a></svg>',
                            ("<svg", "xlink", "evil.example")),
    "a tag inside a value": ('<a title="</a><script>alert(1)</script>">t</a>', ("<script",)),
    "a form": ('<form action="https://evil.example"><input formaction="https://evil.example"></form>',
               ("<form", "action", "evil.example")),
    "a base and a stylesheet": ('<base href="https://evil.example/"><link rel=stylesheet '
                                'href="//evil.example/a.css">', ("<base", "<link", "evil.example")),
}


@pytest.mark.parametrize("name", list(HOSTILE))
def test_markup_written_to_get_past_the_cleaner_does_not(name):
    """The cleaner used to cut patterns out of the text, and what was left
    could close up into the very thing that was cut. It now takes every tag
    apart and writes it again, so nothing comes out that it did not write."""
    from bmail.text import original

    markup, forbidden = HOSTILE[name]
    shown = original.clean(markup).lower()
    for word in forbidden:
        assert word not in shown, (word, shown)


def test_what_a_message_is_made_of_survives_being_rebuilt():
    from bmail.text import original

    markup = ('<table width="560" style="background:#fff"><tr><td align=center bgcolor="#f3f2ef">'
              '<a href="https://jobs.example/all?a=1&amp;b=2" style="color:#0a66c2">See all</a>'
              '<img src="cid-free.png" width=48 height=48 alt="Logo &amp; co">'
              "<style>.pre{display:none} a>b{color:red}</style><p>a &lt; b, 2 > 1 and 3 < 4</p>"
              "<!--[if !mso]><!--><span>shown</span><!--<![endif]--></td></tr></table>")
    assert original.clean(markup) == (
        '<table width="560" style="background:#fff"><tr><td align="center" bgcolor="#f3f2ef">'
        '<a href="https://jobs.example/all?a=1&amp;b=2" style="color:#0a66c2">See all</a>'
        '<img src="cid-free.png" width="48" height="48" alt="Logo &amp; co">'
        "<style>.pre{display:none} a>b{color:red}</style><p>a &lt; b, 2 > 1 and 3 &lt; 4</p>"
        "<span>shown</span></td></tr></table>")
    assert not original.has_remote_pictures(markup)
    assert original.has_remote_pictures('<td style="background:url(https://x.example/b.png)">')
    assert original.has_remote_pictures('<img src="//x.example/b.png">')


@pytest.mark.parametrize("flood", ["<meta ", "<iframe ", "<img ", "<!-- x ", "<script>", '<a title="', "<"])
def test_a_message_written_to_stall_the_reader_is_read_in_one_pass(flood):
    """A cleaner that looked ahead for a ">" from every "<" took half a minute
    on 100 KB of "<img " and would have taken hours on two megabytes, with the
    whole engine stopped: and it ran each time that message was opened."""
    import time

    from bmail.text import original

    markup = flood * (400_000 // len(flood))
    started = time.perf_counter()
    original.page(markup, pictures=True)
    original.has_remote_pictures(markup)
    assert time.perf_counter() - started < 5
