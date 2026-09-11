import pytest

from bmail.ingest.raw import parse_message
from bmail.models import Lane
from bmail.packs import load_pack
from bmail.rules.engine import RulesEngine, coverage
from bmail.rules.headers import header_signals
from bmail.rules.templates import load_recipes, match_template
from bmail.store import repo
from tests.conftest import make_raw


def test_all_shipped_recipes_are_valid():
    recipes = load_recipes()
    assert len(recipes) >= 40
    assert len({r.id for r in recipes}) == len(recipes)  # ids are unique


def test_recipes_are_sorted_by_priority():
    priorities = [r.priority for r in load_recipes()]
    assert priorities == sorted(priorities, reverse=True)


def test_bulk_headers_are_detected():
    message = parse_message(
        make_raw(
            sender="newsletter@shop.example",
            extra={"List-Unsubscribe": "<https://shop.example/u>", "Precedence": "bulk"},
        )
    )
    signals = header_signals(message)
    assert signals.is_bulk and signals.is_list
    assert signals.score >= 0.45


def test_personal_mail_is_not_bulk():
    message = parse_message(make_raw(sender="jonas@gmx.de", sender_name="Jonas Weber"))
    assert header_signals(message).score == 0.0


def test_rejection_outranks_vendor_acknowledgement():
    """The decisive priority rule: every ATS opens its rejections with
    'thank you for your interest', so an outcome phrase must win."""
    message = parse_message(
        make_raw(
            sender="noreply@myworkday.com",
            subject="Update on your application",
            body=(
                "Thank you for your interest in our company. After careful consideration "
                "we have decided not to move forward with your application."
            ),
        )
    )
    hit = match_template(message)
    assert hit is not None
    assert hit.recipe.category == "rejection"


def test_vendor_recipe_still_fires_without_an_outcome_phrase():
    message = parse_message(
        make_raw(
            sender="noreply@myworkday.com",
            subject="Your application",
            body="Thank you for your interest. We have received your application.",
        )
    )
    hit = match_template(message)
    assert hit is not None and hit.recipe.category == "acknowledgement"


def test_body_none_vetoes_a_recipe():
    message = parse_message(
        make_raw(
            body=(
                "Please complete the online assessment. However we have decided not to "
                "move forward with your application."
            )
        )
    )
    hit = match_template(message)
    assert hit is not None and hit.recipe.category != "assessment"


def test_german_rejection_is_matched_by_rule():
    message = parse_message(
        make_raw(
            subject="Ihre Bewerbung",
            body=(
                "Sehr geehrte Bewerberin,\n\nleider müssen wir Ihnen mitteilen, dass wir "
                "Ihre Bewerbung nicht weiter berücksichtigen können.\n\nMit freundlichen Grüßen"
            ),
        )
    )
    hit = match_template(message)
    assert hit is not None and hit.recipe.category == "rejection"


def test_vendor_domain_never_becomes_the_employer():
    message = parse_message(
        make_raw(
            sender="no-reply@greenhouse.io",
            sender_name="Acme GmbH via Greenhouse",
            body="Unfortunately, we have decided not to move forward with your application.",
        )
    )
    hit = match_template(message)
    assert hit is not None
    assert hit.fields.get("org") in (None, "")  # not "Greenhouse"


def test_stage1_resolves_most_of_a_realistic_corpus(conn, pack, tmp_path):
    from bmail.ingest import FileSource, ingest
    from bmail.samples import write_mbox

    mbox, _ = write_mbox(tmp_path, applications=12, noise=20)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    engine = RulesEngine(pack)
    results = [engine.run(conn, message) for message in repo.iter_messages(conn)]
    stats = coverage(results)
    assert stats["resolved_fraction"] >= 0.6  # the design target for Stage 1
    assert stats["templates"] > 0


def test_duplicates_inherit_the_original_verdict(conn, pack):
    """Dedupe runs after template matching, so this uses a body no recipe claims.

    The point being checked is that folding a repeat does not throw its category
    away — a duplicate of a classified message inherits that classification.
    """
    body = (
        "Sehr geehrte Damen und Herren, hier ist die monatliche Zusammenfassung "
        "Ihrer Aktivitaeten in unserem Portal fuer diesen Zeitraum."
    )
    first = parse_message(
        make_raw(message_id="<a@x>", body=body, subject="Zusammenfassung",
                 extra={"List-Unsubscribe": "<https://x.example/u>"})
    )
    second = parse_message(
        make_raw(message_id="<b@x>", body=body, subject="Zusammenfassung",
                 extra={"List-Unsubscribe": "<https://x.example/u>"})
    )
    repo.bulk_upsert(conn, [first, second])
    engine = RulesEngine(pack)
    original = engine.run(conn, first).verdict
    assert original.lane is not Lane.duplicate
    duplicate = engine.run(conn, second)
    assert duplicate.verdict.lane is Lane.duplicate
    assert duplicate.verdict.category == original.category  # folded, not discarded


def test_deterministic_deadline_carries_evidence(conn, pack):
    message = parse_message(
        make_raw(
            subject="Online-Test",
            body=(
                "Guten Tag,\n\nals nächsten Schritt bitten wir Sie, den Online-Test zu "
                "bearbeiten. Bitte schließen Sie den Test bis zum 15.10.2026 ab."
            ),
        )
    )
    repo.bulk_upsert(conn, [message])
    verdict = RulesEngine(pack).run(conn, message).verdict
    assert verdict.deadline is not None and verdict.deadline.isoformat() == "2026-10-15"
    assert verdict.evidence and "15.10.2026" in verdict.evidence
    assert verdict.evidence_ok is True


def test_pack_categories_cover_every_recipe_category():
    """A recipe whose category no pack declares can never fire — catch that here."""
    declared: set[str] = set()
    for name in ("job-seeker", "student-admissions", "hr-recruiter", "freelance-ops"):
        declared |= set(load_pack(name).categories)
    unreachable = {r.category for r in load_recipes()} - declared
    assert not unreachable, f"recipe categories no pack declares: {sorted(unreachable)}"


def test_the_readme_recipe_count_is_accurate():
    """A number in the README that nothing checks is a number that drifts."""
    import re
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    if not readme.exists():  # installed without the repo tree
        pytest.skip("README not present")
    stated = re.search(r"(\d+) starter template recipes", readme.read_text(encoding="utf-8"))
    assert stated, "the README should state how many template recipes ship"
    assert int(stated.group(1)) == len(load_recipes())


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Ihre Bewerbung als Backend Developer bei Nordwind Analytics", "Backend Developer"),
        ("Ihre Bewerbung als Software Engineer (m/w/d)", "Software Engineer (m/w/d)"),
        ("Online-Test für Ihre Bewerbung als Data Engineer", "Data Engineer"),
        ("Einladung zum Vorstellungsgespräch – Data Engineer", "Data Engineer"),
    ],
)
def test_german_subjects_yield_the_role(subject, expected):
    """The role is what separates two applications at the same company."""
    message = parse_message(make_raw(
        subject=subject,
        body="Vielen Dank für Ihre Bewerbung. Wir haben Ihre Bewerbung erhalten.",
    ))
    hit = match_template(message)
    assert hit is not None
    assert hit.fields.get("role") == expected


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Your application for Platform Engineer at Lumen Mobility",
         "Thank you for applying to Lumen Mobility.", "Platform Engineer"),
        ("Update on your application – Machine Learning Engineer",
         "Unfortunately, we have decided not to move forward with your application.",
         "Machine Learning Engineer"),
        ("Next step: online assessment for Backend Developer",
         "Please complete the online assessment within 5 days.", "Backend Developer"),
    ],
)
def test_english_subjects_yield_the_role(subject, body, expected):
    message = parse_message(make_raw(subject=subject, body=body))
    hit = match_template(message)
    assert hit is not None
    assert hit.fields.get("role") == expected


def test_two_roles_at_one_company_stay_separate(conn, pack, tmp_path):
    """Without a role the case key collapses to the organisation alone, and
    every application at that company merges into one — which is exactly the
    question the tracker exists to answer."""
    from bmail.classify.stage2 import Classifier
    from bmail.pipeline import Pipeline

    body = ("Sehr geehrte Bewerberin,\n\nvielen Dank für Ihre Bewerbung. "
            "Wir haben Ihre Bewerbung erhalten und prüfen Ihre Unterlagen.")
    messages = [
        parse_message(make_raw(
            message_id=f"<role{i}@x>", sender="bewerbung@nordwind-analytics.de",
            sender_name="Nordwind Analytics", subject=subject, body=body,
        ))
        for i, subject in enumerate((
            "Ihre Bewerbung als Backend Developer bei Nordwind Analytics",
            "Ihre Bewerbung als Data Engineer bei Nordwind Analytics",
            "Ihre Bewerbung als Software Engineer (m/w/d)",
        ))
    ]
    repo.bulk_upsert(conn, messages)
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(conn, messages)

    cases = repo.list_cases(conn, pack.name)
    roles = {case.role for case in cases}
    assert len(cases) == 3, f"applications merged: {[(c.org, c.role) for c in cases]}"
    assert "Backend Developer" in roles and "Data Engineer" in roles
