"""HTML mail as senders really write it, read the way a mail client shows it.

Every test runs twice: through selectolax and through the stdlib fallback,
which must produce the same text.
"""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from bmail.ingest.raw import parse_message
from bmail.text import html as html_module
from bmail.text.html import hidden_classes, html_to_text

PARSERS = ["stdlib"] + (["selectolax"] if html_module._Selectolax is not None else [])


@pytest.fixture(params=PARSERS, autouse=True)
def parser(request, monkeypatch):
    if request.param == "stdlib":
        monkeypatch.setattr(html_module, "_Selectolax", None)
    return request.param


SPACER = "&zwnj;&nbsp;&#847;" * 80

ATS = f"""<!DOCTYPE html><html><head><title>Interview</title>
<style>.btn{{color:#fff}} @media (max-width:600px){{.desktop{{display:none!important}}}}</style>
<!--[if mso]><xml><o:OfficeDocumentSettings/></xml><![endif]--></head>
<body>
<div style="display:none;max-height:0;overflow:hidden">Your interview is confirmed {SPACER}</div>
<table role="presentation" width="100%"><tr><td align="center"><table width="600">
<tr><td><img src="https://cdn.example/logo.png" alt="Acme"></td>
    <td align="right"><a href="https://acme.example/web">View in browser</a></td></tr>
<tr><td colspan="2" class="desktop"><h1>Interview invitation</h1>
<p>Hi Bhargav,</p>
<p>Thank you for applying for the <strong>Backend Developer</strong> role.</p>
<table><tr><th>Date</th><td>Tuesday, 6 October 2026</td></tr>
       <tr><th>Time</th><td>10:00&ndash;11:00 CEST</td></tr></table>
<p>Please bring:</p><ul><li>your ID</li><li>a copy of your CV</li></ul>
<table><tr><td class="btn"><a href="https://acme.example/confirm">Confirm your slot</a></td>
           <td></td><td><a href="https://acme.example/move">Reschedule</a></td></tr></table>
<p>Kind regards,<br>Jana Schmidt</p></td></tr>
</table></td></tr></table>
<img src="https://track.example/open.gif" width="1" height="1" alt="">
</body></html>"""


def test_a_layout_table_mail_reads_like_the_mail():
    text = html_to_text(ATS)
    assert text.startswith("View in browser\n\nInterview invitation\n\nHi Bhargav,")
    assert "Date · Tuesday, 6 October 2026\nTime · 10:00–11:00 CEST" in text
    assert "• your ID\n• a copy of your CV" in text
    # An empty spacer cell leaves no dangling separator.
    assert "Confirm your slot · Reschedule" in text
    assert "Kind regards,\nJana Schmidt" in text


def test_the_hidden_preheader_and_its_padding_are_not_text():
    text = html_to_text(ATS)
    assert "confirmed" not in text
    assert "‌" not in text and "͏" not in text


def test_a_class_hidden_in_the_head_is_hidden_but_not_one_hidden_only_on_phones():
    html = """<html><head><style>
      /* the preheader */ .preheader, span.pre2 { display: none !important; }
      @media only screen and (max-width: 480px) { .wide { display: none; } }
    </style></head><body><span class="preheader">Preview text</span>
    <span class="pre2">More preview</span><div class="wide">The real content</div></body></html>"""
    assert html_to_text(html) == "The real content"


def test_hidden_classes_reads_only_rules_that_always_hide():
    sheets = [".a{display:none} .b .c{display:none} @media print{.d{display:none}} .e{color:red}"]
    assert hidden_classes(sheets) == {"a"}


def test_if_hiding_would_hide_most_of_the_mail_everything_is_shown():
    # A template that hides its body and shows it with a media query is broken
    # for this reader; better to show everything than to show nothing.
    body = "An important update about your application. " * 10
    html = f'<div style="display:none">{body}</div><p>Footer</p>'
    assert "important update" in html_to_text(html)


def test_numbered_and_nested_lists():
    html = "<ol><li>First</li><li>Second<ul><li>detail</li></ul></li></ol><p>After</p>"
    assert html_to_text(html) == "1. First\n2. Second\n  • detail\n\nAfter"


def test_outlook_list_items_keep_their_bullet_inside_paragraphs():
    assert html_to_text("<ul><li><p>one</p></li><li><p>two</p></li></ul>").startswith("• one")


def test_line_breaks_and_blank_lines_as_gmail_writes_them():
    html = '<div dir="ltr">Sounds good.<div><br></div><div>Best,</div><div>Bhargav</div></div>'
    assert html_to_text(html) == "Sounds good.\n\nBest,\nBhargav"


def test_preformatted_text_keeps_its_lines():
    assert html_to_text("<pre>a  b\n  c</pre>") == "a  b\n  c"


def test_unclosed_paragraphs_and_cells_still_read_in_order():
    html = "<p>one<p>two<table><tr><td>a<td>b<tr><td>c</table>"
    assert html_to_text(html) == "one\n\ntwo\n\na · b\nc"


def test_deeply_nested_layout_does_not_break_the_reader():
    html = "<table><tr><td>" * 400 + "Deep text" + "</td></tr></table>" * 400
    assert html_to_text(html) == "Deep text"


def test_a_long_mail_is_kept_whole():
    html = "".join(f"<p>Paragraph {i}: " + "words " * 50 + "</p>" for i in range(200))
    text = html_to_text(html)
    assert text.count("\n\n") == 199
    assert text.endswith("Paragraph 199: " + " ".join(["words"] * 50))


# --- whole messages ---------------------------------------------------------


def _mail(*, plain=None, html=None, charset="utf-8"):
    if plain is not None and html is not None:
        message = MIMEMultipart("alternative")
        message.attach(MIMEText(plain, "plain", charset))
        message.attach(MIMEText(html, "html", charset))
    else:
        message = MIMEText(html if plain is None else plain, "html" if plain is None else "plain", charset)
    message["Subject"] = "Interview invitation"
    message["From"] = "Acme <talent@acme.example>"
    message["Message-ID"] = "<m1@acme.example>"
    message["Date"] = "Mon, 21 Sep 2026 10:00:00 +0200"
    return message.as_bytes()


def test_a_placeholder_text_part_does_not_hide_the_html_one():
    raw = _mail(plain="To view this email, please use an HTML-capable email client.", html=ATS)
    message = parse_message(raw)
    assert "Backend Developer" in message.body_text
    assert message.body_text.startswith("Interview invitation")   # "View in browser" is cleaned away


def test_a_real_text_part_is_preferred_to_the_html_one():
    plain = "Hi Bhargav,\n\nwe would like to invite you to an interview on Tuesday.\n\nJana"
    html = "<p>Hi Bhargav,</p><p>we would like to invite you to an interview on <b>Tuesday</b>.</p><p>Jana</p>"
    assert parse_message(_mail(plain=plain, html=html)).body_raw_text == plain


def test_an_unknown_charset_still_gives_the_text():
    raw = _mail(plain="Grüße aus München, bitte bis Freitag antworten.", charset="latin-1")
    raw = raw.replace(b'charset="iso-8859-1"', b'charset="x-unknown-8bit"')
    assert parse_message(raw).body_text == "Grüße aus München, bitte bis Freitag antworten."


def test_html_sent_as_plain_text_is_read_as_html():
    raw = _mail(plain="<html><body><p>Your application for <b>Data Engineer</b> was received.</p></body></html>")
    assert parse_message(raw).body_text == "Your application for Data Engineer was received."


def test_invisible_padding_in_a_text_part_is_dropped():
    raw = _mail(plain="Your interview is confirmed" + "‌ ͏" * 50 + "\n\nSee you on Tuesday.")
    assert parse_message(raw).body_text == "Your interview is confirmed\n\nSee you on Tuesday."


@pytest.mark.parametrize("stub", [
    "This message is best viewed in HTML.",
    "This email requires HTML. Please switch your mail client to HTML view.",
    "Diese E-Mail wird am besten in HTML dargestellt.",
])
def test_every_way_of_saying_view_the_html_part_reads_the_html(stub):
    message = parse_message(_mail(plain=stub, html=ATS))
    assert "Backend Developer" in message.body_text
