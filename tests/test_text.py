from datetime import date

import pytest

from bmail.text.clean import clean_body, detect_language, normalise_subject, redact_urls
from bmail.text.dates import find_dates, find_deadline, find_event
from bmail.text.html import html_to_text
from bmail.text.simhash import hamming, simhash


def test_html_drops_script_and_keeps_blocks():
    html = "<html><head><style>p{}</style></head><body><p>One</p><div>Two &amp; three</div></body></html>"
    text = html_to_text(html)
    assert "p{}" not in text
    assert text.split("\n")[0] == "One"
    assert text.strip().endswith("Two & three")


def test_html_keeps_words_as_they_appear_on_screen():
    # "Hallo <b>Welt</b>" must not become "HalloWelt", and "<b>B</b>ewerbung"
    # must not become "B ewerbung": inline markup neither joins nor splits
    # words, exactly as in a mail client.
    assert html_to_text("<p>Hallo <b>Welt</b></p>") == "Hallo Welt"
    assert html_to_text("<p><b>B</b>ewerbung</p>") == "Bewerbung"
    assert html_to_text("<table><tr><td>Date</td><td>Tuesday</td></tr></table>") == "Date · Tuesday"


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


def test_tags_left_in_the_plain_text_are_read_not_shown():
    """Marketing systems build the plain part from the HTML one and leave the
    markup in it: the reader showed `<div align="center" class="mktoImg">`."""
    text = clean_body(
        "Catch up on the latest from the conference.\n"
        '<div align="center" class="mktoImg"><a href="https://x.example/a">'
        '<img src="https://x.example/header.jpg" width="680"></a></div>\n'
        "March 2026 News\n"
        "Read Blog&nbsp;❯")
    assert text == "Catch up on the latest from the conference.\n\nMarch 2026 News\nRead Blog ❯"


def test_a_whole_html_document_sent_as_text_is_read_as_html():
    text = clean_body(
        "<!doctype html><html><head><style>body { color: red }</style></head><body>"
        '<div style="display:none">All you need to know</div>'
        "<p>Markets closed higher today.</p>"
        "<table><tr><td>Index</td><td>81,000</td></tr></table></body></html>")
    assert text == "Markets closed higher today.\n\nIndex · 81,000"


def test_angle_brackets_that_are_not_html_stay():
    code = "The fix: use List<String> and check that a < b.\nSee <https://docs.example/list> for more."
    assert clean_body(code) == code
    # Even beside real markup, which is what a code review notification holds.
    mixed = clean_body('<details><summary>Review</summary></details>\nUse List<String> here.')
    assert "List<String>" in mixed and "<details>" not in mixed


def test_entities_in_plain_text_are_the_characters_they_stand_for():
    assert clean_body("Build &amp; Ship with the CLI\nif (remaining &gt; 0) retry();") == (
        "Build & Ship with the CLI\nif (remaining > 0) retry();")


def test_an_unfilled_mail_merge_field_is_not_shown():
    assert clean_body("%opentrack%\n\nYOUR RECEIPT\nReceipt ID: RCW123 · August 30") == (
        "YOUR RECEIPT\nReceipt ID: RCW123 · August 30")
    # Inside a sentence it is something the sender wrote.
    assert "50%" in clean_body("Save 50% today, and another 10% tomorrow.")


def test_a_link_with_a_query_and_no_path_is_shown_as_its_host():
    link = "https://platform.example?utm_campaign=Welcome+series+%5BJan+2026%5D&utm_content=share&utm_medium=email"
    assert clean_body(f"Get discovered by sharing your work ( {link} )") == (
        "Get discovered by sharing your work ( platform.example )")


def test_the_same_bare_tracking_link_over_and_over_is_dropped():
    tracker = "https://click.news.shop.example/?qs=" + "0a1b2c3d4e5f" * 6
    text = clean_body(f"SHOP\n\n{tracker}\n\nHej Alex,\nlearn, plan, relax.\n{tracker}\n"
                      f"Discover now\n{tracker}\nMore at https://www.shop.example/de/")
    assert text == "SHOP\n\nHej Alex,\nlearn, plan, relax.\nDiscover now\nMore at https://www.shop.example/de/"
    # A link given once is something to follow.
    assert clean_body("Reset your password here:\nhttps://shop.example/reset") == (
        "Reset your password here:\nhttps://shop.example/reset")


def test_a_view_in_browser_line_goes_with_its_link():
    assert clean_body("View message in browser: https://www.net.example/m/-H6KV7Bb\n\n"
                      "Hi Alex,\nDo you know Dr. Example?") == "Hi Alex,\nDo you know Dr. Example?"


def test_preview_padding_made_of_joiners_and_direction_marks_is_dropped():
    padding = " ‍‎‏" * 30
    assert clean_body(f"Together we are stronger{padding}\n\nRead what changes for you.") == (
        "Together we are stronger\n\nRead what changes for you.")
    # Between two characters a joiner is part of what is written: a family emoji.
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    assert family in clean_body(f"See you on Sunday {family} at noon.")


def test_a_link_beside_markup_keeps_every_parameter():
    """An HTML reader takes "&reg" for the registered sign even without its
    semicolon, so a link read as HTML came out as "?id=5(R)ion=de"."""
    text = clean_body("<b>Track</b> <i>it</i>: https://shop.example/t?id=5&region=de&section=3&notify=1")
    assert text == "Track it: https://shop.example/t?id=5&region=de&section=3&notify=1"


def test_a_page_of_unclosed_comments_does_not_stall_the_reader():
    import time

    started = time.perf_counter()
    clean_body("<!-- x " * 30000 + "<b>a</b><b>b</b>")
    assert time.perf_counter() - started < 5
