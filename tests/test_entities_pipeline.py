from datetime import datetime, timedelta, timezone

from sift.classify.stage2 import Classifier
from sift.entities.resolve import (
    case_key,
    is_ats_domain,
    keys_match,
    normalise_org,
    normalise_role,
    org_key,
    token_set_ratio,
)
from sift.entities.stage4 import CaseResolver, refresh_derived
from sift.ingest.raw import parse_message
from sift.models import Verdict
from sift.pipeline import Pipeline
from sift.store import repo
from tests.conftest import make_raw


def test_org_normalisation_collapses_legal_suffixes():
    assert normalise_org("Nordwind Analytics GmbH") == normalise_org("nordwind analytics")
    assert normalise_org("Acme Inc.") == normalise_org("ACME")


def test_org_key_bridges_display_names_and_domains():
    # "Atlas Payments" from a display name and "atlaspayments" from a domain
    # are the same employer and must key identically.
    assert org_key("Atlas Payments") == org_key("atlaspayments")


def test_role_normalisation_strips_german_noise():
    assert normalise_role("Senior Data Engineer (m/w/d)") == normalise_role("Data Engineer")
    assert normalise_role("Bewerbung als Data Engineer bei Acme") == "data engineer"


def test_token_set_ratio_discriminates():
    assert token_set_ratio("data engineer", "engineer data") == 100
    assert token_set_ratio("data engineer", "data scientist") < 85


def test_ats_domains_are_recognised():
    assert is_ats_domain("greenhouse.io")
    assert is_ats_domain("eu.myworkday.com")
    assert not is_ats_domain("nordwind-analytics.de")


def test_keys_match_on_org_but_tolerate_a_missing_role():
    key_fields = ["org_domain", "role"]
    with_role = case_key(key_fields, {"org_domain": "Acme GmbH", "role": "Data Engineer"})
    without = case_key(key_fields, {"org_domain": "acme", "role": ""})
    assert keys_match(with_role, without)


def test_different_employers_never_merge():
    key_fields = ["org_domain", "role"]
    a = case_key(key_fields, {"org_domain": "Acme", "role": "Data Engineer"})
    b = case_key(key_fields, {"org_domain": "Beta", "role": "Data Engineer"})
    assert not keys_match(a, b)


def test_case_key_keeps_its_arity():
    key = case_key(["org_domain", "role"], {"org_domain": "Acme", "role": ""})
    assert key.count("|") == 1  # the empty role is kept, so arity stays stable


def test_bulk_mail_never_opens_a_case(conn, pack):
    message = parse_message(
        make_raw(
            sender="newsletter@shop.example",
            body="Our monthly newsletter. Unsubscribe at any time.",
            extra={"List-Unsubscribe": "<https://shop.example/u>"},
        )
    )
    repo.bulk_upsert(conn, [message])
    verdict = Verdict(message_id=message.message_id, pack=pack.name, category="newsletter")
    assert CaseResolver(pack).resolve(conn, message, verdict).case is None


def test_uncertain_verdicts_never_mint_a_case(conn, pack):
    message = parse_message(make_raw(sender="hr@acme.de", body="Some ambiguous text."))
    repo.bulk_upsert(conn, [message])
    verdict = Verdict(
        message_id=message.message_id, pack=pack.name,
        category="acknowledgement", needs_review=True,
    )
    assert CaseResolver(pack).resolve(conn, message, verdict).case is None


def test_a_person_at_a_consumer_mailbox_is_not_an_employer(conn, pack):
    message = parse_message(make_raw(sender="jonas@gmx.de", sender_name="Jonas Weber"))
    repo.bulk_upsert(conn, [message])
    verdict = Verdict(message_id=message.message_id, pack=pack.name, category="acknowledgement")
    assert CaseResolver(pack).resolve(conn, message, verdict).case is None


def test_a_thread_lands_on_one_case(conn, pack):
    resolver = CaseResolver(pack)
    first = parse_message(
        make_raw(message_id="<1@x>", sender="hr@acme.de", subject="Your application")
    )
    reply = parse_message(
        make_raw(
            message_id="<2@x>", sender="hr@acme.de", subject="Re: Your application",
            extra={"References": "<1@x>", "In-Reply-To": "<1@x>"},
        )
    )
    repo.bulk_upsert(conn, [first, reply])
    opened = resolver.resolve(
        conn, first,
        Verdict(message_id="<1@x>", pack=pack.name, category="acknowledgement",
                org="Acme", role="Data Engineer"),
    )
    # The reply carries no extracted role at all; the References chain still binds it.
    followed = resolver.resolve(
        conn, reply, Verdict(message_id="<2@x>", pack=pack.name, category="rejection")
    )
    assert opened.case is not None and followed.case is not None
    assert opened.case.id == followed.case.id
    assert followed.case.state == "rejected"


def test_states_move_forward_only(conn, pack):
    resolver = CaseResolver(pack)
    message = parse_message(make_raw(message_id="<1@x>", sender="hr@acme.de"))
    repo.bulk_upsert(conn, [message])
    resolver.resolve(
        conn, message,
        Verdict(message_id="<1@x>", pack=pack.name, category="offer", org="Acme", role="DE"),
    )
    late = parse_message(make_raw(message_id="<2@x>", sender="hr@acme.de"))
    repo.bulk_upsert(conn, [late])
    result = resolver.resolve(
        conn, late,
        Verdict(message_id="<2@x>", pack=pack.name, category="acknowledgement",
                org="Acme", role="DE"),
    )
    assert result.case is not None
    assert result.case.state == "offer"  # a late acknowledgement cannot undo an offer


def test_ghosting_is_detected_after_the_pack_window(conn, pack):
    old = datetime.now(timezone.utc) - timedelta(days=pack.stall_days + 10)
    message = parse_message(
        make_raw(
            message_id="<1@x>", sender="hr@acme.de",
            date=old.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        )
    )
    repo.bulk_upsert(conn, [message])
    CaseResolver(pack).resolve(
        conn, message,
        Verdict(message_id="<1@x>", pack=pack.name, category="acknowledgement",
                org="Acme", role="Data Engineer"),
    )
    conn.commit()
    refresh_derived(conn, pack)
    cases = repo.list_cases(conn, pack.name)
    assert cases and cases[0].stalled
    assert cases[0].state == "ghosted"


def test_full_pipeline_on_the_sample_corpus(conn, pack, tmp_path):
    from sift.ingest import FileSource, ingest
    from sift.samples import write_mbox

    mbox, _labels = write_mbox(tmp_path, applications=12, noise=16)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    stats = Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(
        conn, repo.iter_messages(conn)
    )
    assert stats.processed > 0
    assert not stats.errors
    assert stats.cases_created > 0
    # Cases must be far fewer than messages, or entity resolution is not working.
    assert stats.cases_created < stats.processed / 2


def test_triage_is_idempotent(conn, pack, tmp_path):
    from sift.ingest import FileSource, ingest
    from sift.samples import write_mbox

    mbox, _ = write_mbox(tmp_path, applications=6, noise=8)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    messages = repo.iter_messages(conn)
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, messages)
    first = len(repo.list_cases(conn, pack.name))
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, messages)
    assert len(repo.list_cases(conn, pack.name)) == first  # no duplicate cases on re-run
