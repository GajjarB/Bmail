from datetime import datetime, timedelta, timezone

from bmail.classify.stage2 import Classifier
from bmail.entities.resolve import (
    case_key,
    is_ats_domain,
    keys_match,
    normalise_org,
    normalise_role,
    org_key,
    token_set_ratio,
)
from bmail.entities.stage4 import CaseResolver, refresh_derived
from bmail.ingest.raw import parse_message
from bmail.models import Verdict
from bmail.pipeline import Pipeline
from bmail.store import repo
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


def test_keys_match_on_org_and_fuzzy_role():
    key_fields = ["org_domain", "role"]
    left = case_key(key_fields, {"org_domain": "Acme GmbH", "role": "Data Engineer (m/w/d)"})
    right = case_key(key_fields, {"org_domain": "acme", "role": "Senior Data Engineer"})
    assert keys_match(left, right)


def test_a_missing_role_does_not_swallow_a_known_one():
    """The regression that made every application to one employer one case."""
    key_fields = ["org_domain", "role"]
    known = case_key(key_fields, {"org_domain": "Acme GmbH", "role": "Data Engineer"})
    blank = case_key(key_fields, {"org_domain": "acme", "role": ""})
    other = case_key(key_fields, {"org_domain": "acme", "role": "Backend Developer"})
    assert not keys_match(known, blank)
    assert not keys_match(blank, known)
    assert not keys_match(known, other)
    assert keys_match(blank, case_key(key_fields, {"org_domain": "acme", "role": None}))


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
    verdict = Verdict(message_id=message.message_id, pack=pack.name,
                      category="newsletter", confidence=0.9)
    assert CaseResolver(pack).resolve(conn, message, verdict).case is None


def test_a_confident_verdict_flagged_for_review_still_opens_a_case(conn, pack):
    message = parse_message(make_raw(sender="hr@acme.de", body="Some ambiguous text."))
    repo.bulk_upsert(conn, [message])
    # Confident, but flagged for a human to confirm: that alone must not block a
    # case, or a pack whose opening category always escalates can never track.
    verdict = Verdict(
        message_id=message.message_id, pack=pack.name,
        category="acknowledgement", confidence=0.93, needs_review=True,
        org="Acme", role="Data Engineer",
    )
    assert CaseResolver(pack).resolve(conn, message, verdict).case is not None


def test_a_person_at_a_consumer_mailbox_is_not_an_employer(conn, pack):
    message = parse_message(make_raw(sender="jonas@gmx.de", sender_name="Jonas Weber"))
    repo.bulk_upsert(conn, [message])
    verdict = Verdict(message_id=message.message_id, pack=pack.name,
                      category="acknowledgement", confidence=0.9)
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
                org="Acme", role="Data Engineer", confidence=0.9),
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
        Verdict(message_id="<1@x>", pack=pack.name, category="offer", org="Acme", role="DE",
                confidence=0.9),
    )
    late = parse_message(make_raw(message_id="<2@x>", sender="hr@acme.de"))
    repo.bulk_upsert(conn, [late])
    result = resolver.resolve(
        conn, late,
        Verdict(message_id="<2@x>", pack=pack.name, category="acknowledgement",
                org="Acme", role="DE", confidence=0.9),
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
                org="Acme", role="Data Engineer", confidence=0.9),
    )
    conn.commit()
    refresh_derived(conn, pack)
    cases = repo.list_cases(conn, pack.name)
    assert cases and cases[0].stalled
    assert cases[0].state == "ghosted"


def test_full_pipeline_on_the_sample_corpus(conn, pack, tmp_path):
    from bmail.ingest import FileSource, ingest
    from bmail.samples import write_mbox

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
    from bmail.ingest import FileSource, ingest
    from bmail.samples import write_mbox

    mbox, _ = write_mbox(tmp_path, applications=6, noise=8)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    messages = repo.iter_messages(conn)
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, messages)
    first = len(repo.list_cases(conn, pack.name))
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, messages)
    assert len(repo.list_cases(conn, pack.name)) == first  # no duplicate cases on re-run


def test_a_human_correction_survives_a_re_triage(conn, pack, tmp_path):
    """Re-triaging must not quietly undo an answer someone typed in."""
    from bmail.ingest import FileSource, ingest
    from bmail.models import Stage
    from bmail.samples import write_mbox

    mbox, _ = write_mbox(tmp_path, applications=4, noise=4)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    messages = repo.iter_messages(conn)
    pipeline = Pipeline(pack, classifier=Classifier(pack), use_llm=False)
    pipeline.triage(conn, messages)

    target = messages[0].message_id
    original = repo.get_verdict(conn, target, pack.name)
    assert original is not None and original.category != "personal"
    repo.correct_verdict(conn, target, pack.name, "personal", True)

    pipeline.triage(conn, messages)  # re-triage everything
    kept = repo.get_verdict(conn, target, pack.name)
    assert kept is not None
    assert kept.category == "personal"
    assert kept.stage is Stage.manual


def test_the_correction_reaches_the_case_machinery(conn, pack, tmp_path):
    """Honouring a manual verdict must not skip entity resolution for it."""
    from bmail.ingest import FileSource, ingest
    from bmail.samples import write_mbox

    mbox, _ = write_mbox(tmp_path, applications=4, noise=4)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    messages = repo.iter_messages(conn)
    pipeline = Pipeline(pack, classifier=Classifier(pack), use_llm=False)
    pipeline.triage(conn, messages)
    repo.correct_verdict(conn, messages[0].message_id, pack.name, "rejection", False)

    stats = pipeline.triage(conn, messages)
    assert stats.processed == len(messages)  # the manual one is still counted, not skipped


def test_evaluation_ignores_human_corrections(conn, pack, tmp_path):
    """Otherwise the harness would score the labels, not the pipeline."""
    from bmail.evaluation import Dataset, evaluate
    from bmail.ingest import FileSource, ingest
    from bmail.samples import write_mbox

    mbox, labels = write_mbox(tmp_path, applications=4, noise=6)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    messages = repo.iter_messages(conn)
    pipeline = Pipeline(pack, classifier=Classifier(pack), use_llm=False)
    pipeline.triage(conn, messages)
    for message in messages:
        repo.correct_verdict(conn, message.message_id, pack.name, "offer", True)

    report = evaluate(conn, pack, pipeline, Dataset.load(labels))
    assert report.category.per_class.get("offer") is None or (
        report.category.per_class["offer"].predicted < report.evaluated
    ), "the planted manual labels leaked into the evaluation"
    assert pipeline.respect_manual is True  # restored afterwards


def _mail(subject, sender, name, body, days_ago, message_id):
    from email.message import EmailMessage
    from email.utils import format_datetime

    message = EmailMessage()
    message["Message-ID"] = message_id
    message["Subject"] = subject
    message["From"] = f"{name} <{sender}>"
    message["To"] = "you@example.com"
    message["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))
    message.set_content(body)
    return message


def _triage_corpus(tmp_path, pack_name, rows):
    from bmail.ingest import FileSource, ingest
    from bmail.packs import load_pack
    from bmail.store.db import connect

    box = tmp_path / f"{pack_name}.mbox"
    with box.open("w", encoding="utf-8") as handle:
        for row in rows:
            message = _mail(*row)
            handle.write(f"From bmail@x {message['Date']}\n{message}\n")
    conn = connect(tmp_path / f"{pack_name}.sqlite3")
    ingest(conn, FileSource(box, name="t", own_addresses={"you@example.com"}))
    pack = load_pack(pack_name)
    stats = Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(
        conn, repo.iter_messages(conn)
    )
    return conn, pack, stats


def test_hr_recruiter_tracks_candidates_without_a_local_model(tmp_path):
    """Its case-opening category is high-stakes, so it always escalates.

    When no model is there to escalate to, the verdict is flagged for review
    while still being perfectly confident — and a guard keyed on that flag left
    this pack unable to track anything at all.
    """
    conn, _pack, _stats = _triage_corpus(tmp_path, "hr-recruiter", [
        ("Bewerbung als Data Engineer", "anna.klein@gmx.de", "Anna Klein",
         "hiermit bewerbe ich mich auf die ausgeschriebene Stelle. Anbei mein Lebenslauf.",
         30, "<hr1@x>"),
        ("Re: Bewerbung als Data Engineer", "anna.klein@gmx.de", "Anna Klein",
         "Thank you for getting back to me. Tuesday afternoon works well for a call.",
         20, "<hr2@x>"),
        ("Withdrawing my application", "tom.becker@web.de", "Tom Becker",
         "I would like to withdraw my application as I have accepted another offer.",
         2, "<hr3@x>"),
    ])
    try:
        cases = repo.list_cases(conn, "hr-recruiter")
        assert cases, "no candidate was tracked at all"
        assert any(case.message_count > 1 for case in cases), "a thread should land on one case"
        assert any(case.closed for case in cases), "a withdrawal closes the case"
    finally:
        conn.close()


def test_freelance_ops_tracks_invoices(tmp_path):
    conn, _pack, _stats = _triage_corpus(tmp_path, "freelance-ops", [
        ("Rechnung 2026-014", "billing@acme-gmbh.de", "Acme GmbH",
         "Anbei erhalten Sie die Rechnung 2026-014 über 1.250,00 EUR, zahlbar innerhalb von 14 Tagen.",
         40, "<fo1@x>"),
        ("Zahlungserinnerung", "billing@acme-gmbh.de", "Acme GmbH",
         "Zahlungserinnerung: die Rechnung 2026-014 ist seit 21 Tagen überfällig.", 5, "<fo2@x>"),
        ("Invoice 2026-020", "ap@northwind.example", "Northwind AP",
         "Please find attached invoice 2026-020 for EUR 3,400.00, due within 14 days.",
         25, "<fo3@x>"),
        ("Payment sent", "ap@northwind.example", "Northwind AP",
         "The payment for invoice 2026-020 has been transferred today.", 1, "<fo4@x>"),
    ])
    try:
        cases = repo.list_cases(conn, "freelance-ops")
        states = {case.state for case in cases}
        assert "overdue" in states, "a dunning notice should move an invoice overdue"
        assert "paid" in states, "a payment confirmation should close one"
    finally:
        conn.close()


def test_a_low_confidence_guess_still_never_mints_a_case(conn, pack, tmp_path):
    """The guard's original purpose: a 0.14-confidence guess is not an entity."""
    from bmail.entities.stage4 import MIN_CONFIDENCE_TO_OPEN
    from bmail.models import Verdict

    message = parse_message(make_raw(sender="jonas@gmx.de", sender_name="Jonas Weber"))
    repo.bulk_upsert(conn, [message])
    verdict = Verdict(
        message_id=message.message_id, pack=pack.name,
        category="acknowledgement", confidence=MIN_CONFIDENCE_TO_OPEN - 0.1,
    )
    assert CaseResolver(pack).resolve(conn, message, verdict).case is None


def test_every_shipped_pack_can_open_a_case_without_a_model(tmp_path):
    """A pack whose opening category always escalates must still work offline."""
    from bmail.entities.stage4 import MIN_CONFIDENCE_TO_OPEN
    from bmail.packs import available_packs, load_pack

    for name in available_packs():
        pack = load_pack(name)
        openers = [
            category for category, spec in pack.categories.items()
            if spec.opens_case or (spec.state and spec.state == pack.initial_state)
        ]
        assert openers, f"pack '{name}' has no category that can open a case"
    assert 0.0 < MIN_CONFIDENCE_TO_OPEN < 1.0


def test_three_roles_at_one_employer_stay_three_cases(conn, pack):
    """Three applications to the same institute, one of them with no role in the
    mail at all. They must not collapse into a single card."""
    resolver = CaseResolver(pack)
    roles = [None, "Backend Developer", "Data Engineer"]
    cases = []
    for index, role in enumerate(roles):
        message = parse_message(
            make_raw(
                message_id=f"<apply-{index}@fraunhofer-ipt.de>",
                sender=f"noreply-{index}@fraunhofer-ipt.de",
                subject=f"Ihre Bewerbung {index}",
                body="Vielen Dank für Ihre Bewerbung.",
            )
        )
        repo.bulk_upsert(conn, [message])
        verdict = Verdict(
            message_id=message.message_id, pack=pack.name,
            category="acknowledgement", confidence=0.9,
            org="Fraunhofer IPT", role=role,
        )
        resolved = resolver.resolve(conn, message, verdict)
        assert resolved.case is not None
        cases.append(resolved.case.id)
    assert len(set(cases)) == 3


# --------------------------------------------------------------------------- #
# When nothing settles a message
# --------------------------------------------------------------------------- #


def _coffee(message_id="<coffee@gmx.de>"):
    return parse_message(make_raw(
        subject="Kaffee am Freitag?", sender="jonas.weber@gmx.de", sender_name="Jonas Weber",
        body="Hallo, schön, dich letzte Woche getroffen zu haben. "
             "Hast du am Freitag Zeit für einen Kaffee?",
        message_id=message_id,
    ), own_addresses={"you@example.com"})


def test_an_unsure_guess_about_a_persons_mail_is_shown_not_filed(conn, pack):
    """With no local model, the day-one classifier guessed "acknowledgement"
    at p=0.14 for a friend's question, and that guess was final — so the one
    message that most needed a reply was filed as needing nothing."""
    message = _coffee()
    repo.bulk_upsert(conn, [message])
    stats = Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, [message])
    verdict = repo.get_verdict(conn, message.message_id, pack.name)
    assert verdict.category == "personal"
    assert verdict.action_required
    assert verdict.needs_review, "it must still reach the review queue, to teach the classifier"
    assert stats.processed == 1


def test_an_unsure_guess_about_machine_mail_is_left_alone(conn, pack):
    message = parse_message(make_raw(
        subject="Your weekly digest", sender="noreply@updates.example.com",
        sender_name="Updates",
        body="Here is what happened this week on the platform.",
        extra={"List-Unsubscribe": "<mailto:unsub@updates.example.com>",
               "Precedence": "bulk", "Auto-Submitted": "auto-generated"},
        message_id="<digest@updates.example.com>",
    ))
    repo.bulk_upsert(conn, [message])
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, [message])
    assert repo.get_verdict(conn, message.message_id, pack.name).category != "personal"


def test_a_repeat_does_not_inherit_an_unsure_answer_from_an_earlier_run(conn, pack):
    """Near-duplicates copy the verdict of what they repeat. Copying an unsure
    guess from the store made every repeat inherit the mistake — and because
    each re-sort found the other's stale answer, re-sorting never cleared it."""
    first, repeat = _coffee("<coffee-1@gmx.de>"), _coffee("<coffee-2@gmx.de>")
    repo.bulk_upsert(conn, [first, repeat])
    stale = Verdict(message_id=first.message_id, pack=pack.name, category="acknowledgement",
                    confidence=0.14, needs_review=True)
    repo.save_verdict(conn, stale)
    conn.commit()
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, [repeat])
    assert repo.get_verdict(conn, repeat.message_id, pack.name).category == "personal"


def test_a_repeat_still_inherits_a_human_correction(conn, pack):
    from bmail.models import Stage

    first, repeat = _coffee("<coffee-1@gmx.de>"), _coffee("<coffee-2@gmx.de>")
    repo.bulk_upsert(conn, [first, repeat])
    corrected = Verdict(message_id=first.message_id, pack=pack.name, category="other",
                        confidence=1.0, stage=Stage.manual)
    repo.save_verdict(conn, corrected)
    conn.commit()
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, [repeat])
    assert repo.get_verdict(conn, repeat.message_id, pack.name).category == "other"
