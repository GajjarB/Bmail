"""What "needs you" means: still open, not merely recent.

Each test builds the smallest mailbox that exercises one rule, triages it with
the real pipeline (no model), and reads the Brief as of a fixed moment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from bmail.classify.stage2 import Classifier
from bmail.ingest.raw import parse_message
from bmail.outputs.brief import FOLLOW_UP_WITHIN_DAYS, build_brief, render_text
from bmail.pipeline import Pipeline
from bmail.store import repo
from tests.conftest import make_raw

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
ME = "you@example.com"


def mail(subject, body, *, sender="talent@acme-example.com", name="Acme Talent",
         days_ago=1.0, mid=None, extra=None):
    sent = NOW - timedelta(days=days_ago)
    return parse_message(make_raw(
        subject=subject, body=body, sender=sender, sender_name=name,
        date=format_datetime(sent), message_id=mid or f"<{abs(hash((subject, days_ago)))}@x>",
        extra=extra,
    ), own_addresses={ME})


def triage(conn, pack, *messages):
    repo.bulk_upsert(conn, list(messages))
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, list(messages))


def needs(conn, pack, **kwargs):
    return build_brief(conn, pack, now=NOW, **kwargs)


INVITE = ("Interview invitation - Backend Developer",
          "We would like to invite you to an interview for the Backend Developer role.")
ASSESS = ("Assessment for Backend Developer",
          "Please complete the online assessment for the Backend Developer role.")
REJECT = ("Your application for Backend Developer",
          "Unfortunately, we have decided not to move forward with your application.")
COFFEE = ("Coffee on Friday?", "Hi, great to meet you last week. Are you free for a coffee on Friday?")


def test_an_unanswered_request_outlives_the_window(conn, pack):
    """It used to be "arrived in the last day": a five-day-old invitation you
    had not answered simply disappeared from the Brief."""
    triage(conn, pack, mail(*INVITE, days_ago=5))
    brief = needs(conn, pack, window_days=1)
    assert [item.title for item in brief.needs_you] == [INVITE[0]]


def test_replying_in_the_thread_settles_a_request(conn, pack):
    ask = mail(*COFFEE, sender="jonas@gmx.example", name="Jonas", days_ago=3, mid="<ask@x>")
    triage(conn, pack, ask)
    assert needs(conn, pack).counts["needs_you"] == 1
    reply = mail("Re: Coffee on Friday?", "Friday works, see you then.", sender=ME, name="Me",
                 days_ago=2, mid="<reply@x>",
                 extra={"In-Reply-To": "<ask@x>", "References": "<ask@x>"})
    repo.bulk_upsert(conn, [reply])   # your own mail is stored, never triaged
    assert needs(conn, pack).counts["needs_you"] == 0


def test_a_later_mail_on_the_same_case_settles_a_request(conn, pack):
    """An assessment you were asked to take stops needing you once the same
    application has moved on — here, to a rejection."""
    triage(conn, pack, mail(*ASSESS, days_ago=10))
    assert needs(conn, pack).counts["needs_you"] == 1
    triage(conn, pack, mail(*REJECT, days_ago=2))
    assert needs(conn, pack).counts["needs_you"] == 0


def test_copies_of_one_request_are_one_thing_to_do(conn, pack):
    copies = [mail(*COFFEE, sender="jonas@gmx.example", name="Jonas", days_ago=d, mid=f"<c{d}@x>")
              for d in (1, 2, 3)]
    triage(conn, pack, *copies)
    brief = needs(conn, pack)
    assert brief.counts["needs_you"] == 1
    assert brief.needs_you[0].message_id == "<c1@x>", "the newest copy stands for the rest"


def test_a_request_with_no_date_ages_out(conn, pack):
    triage(conn, pack, mail(*COFFEE, sender="jonas@gmx.example", name="Jonas", days_ago=45))
    assert needs(conn, pack).counts["needs_you"] == 0


def test_a_deadline_is_counted_as_one_even_without_a_case(conn, pack):
    """"0 deadlines" used to head a Brief with two overdue ones in it, because
    only deadlines held by a case were counted."""
    triage(conn, pack, mail(
        "Your application to MSc Computer Science",
        "Your application is incomplete. Please upload your transcript "
        f"no later than {(NOW + timedelta(days=5)).strftime('%d %B %Y')}.",
        sender="admissions@uni-example.nl", name="Admissions", days_ago=2))
    brief = needs(conn, pack)
    assert brief.counts["needs_you"] == 1
    assert brief.counts["deadlines"] == 1


def test_follow_ups_are_suggested_only_while_worth_sending(conn, pack):
    """Stalled cases used to be listed oldest first, so an application silent
    for four months headed a list called "worth one follow-up"."""
    triage(conn, pack,
           mail("Application received - Data Engineer", "Thank you for your application. "
                "We have received your application for Data Engineer.", days_ago=30,
                sender="jobs@fresh-example.com", name="Fresh"),
           mail("Application received - Data Engineer", "Thank you for your application. "
                "We have received your application for Data Engineer.", days_ago=45,
                sender="jobs@older-example.com", name="Older"),
           mail("Application received - Data Engineer", "Thank you for your application. "
                "We have received your application for Data Engineer.",
                days_ago=FOLLOW_UP_WITHIN_DAYS + 40, sender="jobs@gone-example.com", name="Gone"))
    from bmail.entities.stage4 import refresh_derived
    refresh_derived(conn, pack, now=NOW)
    brief = needs(conn, pack)
    silences = [int(item.score) for item in brief.stalled]
    assert silences == sorted(silences), "the freshest silence comes first"
    assert all(s <= FOLLOW_UP_WITHIN_DAYS for s in silences)
    assert brief.counts["quiet"] >= len(brief.stalled) + 1, "the old one is counted, not suggested"


def test_one_thing_is_singular(conn, pack):
    triage(conn, pack, mail(*INVITE, days_ago=1))
    assert "1 needs you" in render_text(needs(conn, pack))
