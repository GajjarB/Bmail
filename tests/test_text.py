from datetime import date

import pytest

from sift.text.clean import clean_body, detect_language, normalise_subject, redact_urls
from sift.text.dates import find_dates, find_deadline, find_event
from sift.text.html import html_to_text
from sift.text.simhash import hamming, simhash


def test_html_drops_script_and_keeps_blocks():
    html = "<html><head><style>p{}</style></head><body><p>One</p><div>Two &amp; three</div></body></html>"
    text = html_to_text(html)
    assert "p{}" not in text
    assert text.split("\n")[0] == "One"
    assert text.strip().endswith("Two & three")


def test_html_keeps_inline_words_apart():
    # "Hallo <b>Welt</b>" must not become "HalloWelt": fused tokens break both
    # search and the classifier's features.
    assert html_to_text("<p>Hallo <b>Welt</b></p>") == "Hallo Welt"
    assert html_to_text("<p>Hello<span>world</span></p>") == "Hello world"


def test_html_survives_broken_markup():
    assert "Hello" in html_to_text("<div><p>Hello<span>world")


@pytest.mark.parametrize(
    "raw",
    [
        "Thanks!\n\nOn Mon, 1 Jan 2026 at 10:00, Bob <b@x.com> wrote:\n> old\n> older",
        "Thanks!\n\nAm 01.01.2026 um 10:00 schrieb Bob:\n> alt",
        "Thanks!\n\n-----Original Message-----\nFrom: Bob\nSent: yesterday",
    ],
)
def test_quoted_history_is_removed(raw):
    assert clean_body(raw) == "Thanks!"


def test_short_message_keeps_its_greeting():
    # A greeting phrase near the start must not be mistaken for a signature.
    text = "Mit freundlichen Grüßen, könnten Sie mir bitte den Termin bestätigen?"
    assert "Termin" in clean_body(text)


def test_normalise_subject_strips_repeated_prefixes():
    assert normalise_subject("Re: AW: Fwd: [EXTERNAL] Ihre Bewerbung") == "ihre bewerbung"


def test_language_detection_both_ways():
    assert detect_language("Leider müssen wir Ihnen mitteilen, dass wir Ihre Bewerbung ablehnen.") == "de"
    assert detect_language("Thank you for your application for the position.") == "en"
    assert detect_language("x") == "und"


def test_urls_are_reduced_to_hosts():
    assert redact_urls("click https://tracker.example.com/a/b?c=1 now") == (
        "click [link:tracker.example.com] now"
    )


def test_simhash_ignores_template_variables():
    a = simhash("We received your application 12345 for Data Engineer")
    b = simhash("We received your application 99887 for Data Engineer")
    c = simhash("Completely different content about invoices and payment terms")
    assert hamming(a, b) <= 3
    assert hamming(a, c) > 3


def test_simhash_fits_a_sqlite_integer():
    assert simhash("anything at all") < 2**63


class TestDates:
    reference = date(2026, 9, 1)

    def test_german_deadline(self):
        hit = find_deadline("Bitte reichen Sie die Unterlagen bis zum 15.10.2026 ein.",
                            reference=self.reference)
        assert hit and hit.value == date(2026, 10, 15)

    def test_english_deadline(self):
        hit = find_deadline("The application deadline is 15 July 2026.", reference=self.reference)
        assert hit and hit.value == date(2026, 7, 15)

    def test_relative_deadline(self):
        hit = find_deadline("Please complete the assessment within 5 days.",
                            reference=self.reference)
        assert hit and hit.value == date(2026, 9, 6)

    def test_uncued_date_is_not_a_deadline(self):
        # A reference number that looks like a date must not become a deadline.
        assert find_deadline("Reference 12.09.2019 received.", reference=self.reference) is None

    def test_date_is_not_read_as_a_time(self):
        hits = find_dates("Frist: 15.10.2026", reference=self.reference)
        assert hits and hits[0].at is None

    def test_event_with_named_timezone(self):
        hit = find_event("We propose 14 July 2026 at 10:00 CET.", reference=self.reference)
        assert hit and hit.as_utc().hour == 9

    def test_event_with_explicit_offset(self):
        hit = find_event("Call on 14 July 2026 at 10:00 UTC+2", reference=self.reference)
        assert hit and hit.as_utc().hour == 8

    def test_german_event_time(self):
        hit = find_event("Termin: 14. September 2026 um 14:30 Uhr", reference=self.reference)
        assert hit and hit.value == date(2026, 9, 14) and hit.at.hour == 14
