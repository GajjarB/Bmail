"""How Bmail sorts a realistic inbox: see tests/realworld.py.

Accuracy is allowed to slip a little as recipes change; the three failures
that make the app untrustworthy are not allowed at all: housekeeping shown as
needing you, something that needs you filed away, and mail about a credit card
or a DSL order read as the outcome of a job application.
"""

from __future__ import annotations

import pytest

from bmail.classify.stage2 import Classifier
from bmail.ingest.raw import parse_message
from bmail.pipeline import Pipeline
from bmail.store import repo
from tests.realworld import CASES, HOLDOUT, ME

JOB_OUTCOMES = {
    "acknowledgement", "rejection", "interview_invite", "assessment", "offer",
    "document_request", "scheduling",
}


def sort(conn, pack, cases):
    messages = [parse_message(case.raw(index), own_addresses={ME}) for index, case in enumerate(cases)]
    repo.bulk_upsert(conn, messages)
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, repo.iter_messages(conn))
    return [(case, repo.get_verdict(conn, message.message_id, pack.name))
            for case, message in zip(cases, messages, strict=True)]


@pytest.mark.parametrize("cases", [CASES, HOLDOUT], ids=["inbox", "holdout"])
def test_a_realistic_inbox_is_sorted_the_way_a_person_would(conn, pack, cases):
    results = sort(conn, pack, cases)

    wrong = [f"{case.name}: {verdict.category}" for case, verdict in results
             if verdict.category not in case.expect]
    assert len(wrong) <= len(cases) * 0.05, wrong

    shown_for_nothing = [case.name for case, verdict in results
                         if case.needs_you is False and verdict.action_required]
    assert shown_for_nothing == []

    missed = [case.name for case, verdict in results
              if case.needs_you is True and not verdict.action_required]
    assert missed == []

    mistaken_for_a_job = [f"{case.name}: {verdict.category}" for case, verdict in results
                          if not case.job and verdict.category in JOB_OUTCOMES]
    assert mistaken_for_a_job == []


def test_the_words_a_bank_or_an_office_uses_do_not_make_it_about_a_job(pack):
    for text in (
        "we regret to inform you that we are unable to process your Credit Card application",
        "We confirm that your investment application under your folio is processed",
        "Application Île-de-France Mobilités Votre justificatif d'achat",
        "Dear Applicant, your appointment for the visa application has been confirmed",
        "den genauen Termin kann ich Ihnen leider noch nicht nennen",
    ):
        assert not pack.is_on_topic(text), text
    for text in (
        "Thank you for your application at Amazon. We will contact you when a vacancy opens.",
        "Your application for Backend Developer",
        "vielen Dank für Ihre Bewerbung als IT-Administrator",
    ):
        assert pack.is_on_topic(text), text
