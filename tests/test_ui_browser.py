"""The UI, driven in a real browser.

Skipped unless Playwright and a Chromium build are both available, because the
suite must stay runnable on a plain checkout. When it does run, it is the only
thing that proves the page actually works rather than merely parses.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import pytest

from bmail.api import build_server
from bmail.config import Config
from bmail.ingest import FileSource, ingest
from bmail.samples import write_mbox
from bmail.store import repo
from bmail.store.db import connect

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


def _chromium_path() -> str | None:
    """A Chromium that was installed outside Playwright's own cache, if any."""
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    for candidate in sorted(root.glob("chromium*/chrome-linux/chrome")):
        return str(candidate)
    return None


def launch_chromium(playwright):
    """Chromium from a preinstalled build, else from `playwright install`.

    Skips rather than fails when neither exists: the suite must stay runnable
    on a plain checkout. CI installs a browser, so there the tests run.
    """
    executable = _chromium_path()
    try:
        return playwright.chromium.launch(executable_path=executable, args=["--no-sandbox"])
    except Exception as exc:  # no browser installed
        pytest.skip(f"no Chromium build available ({type(exc).__name__})")


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    from bmail.classify.stage2 import Classifier
    from bmail.packs import load_pack
    from bmail.pipeline import Pipeline

    tmp_path = tmp_path_factory.mktemp("ui")
    home = tmp_path / "home"
    home.mkdir()
    config = Config(home=home, db_path=home / "bmail.sqlite3")
    mbox, _labels = write_mbox(tmp_path, applications=10, noise=12)

    conn = connect(config.database)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    pack = load_pack("job-seeker")
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(
        conn, repo.iter_messages(conn)
    )
    conn.close()

    server, app = build_server(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", app, config
    server.shutdown()
    server.server_close()
    app.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def page_factory(ui):
    base, app, config = ui
    with playwright_api.sync_playwright() as p:
        browser = launch_chromium(p)

        def make(**kwargs):
            kwargs.setdefault("viewport", {"width": 1440, "height": 900})
            page = browser.new_page(**kwargs)
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            # Console errors too, not just uncaught exceptions. A Content-Security-
            # Policy violation is reported here and nowhere else, which is how
            # eleven dead inline styles sat in the UI without a single test
            # noticing that the browser was refusing every one of them.
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(f"{base}/?token={app.token}", wait_until="networkidle")
            page.wait_for_selector("#shell:not([hidden])", timeout=15000)
            return page, errors, config

        yield make
        browser.close()


def test_the_app_boots_and_hides_the_token(page_factory):
    page, errors, _config = page_factory()
    assert "token=" not in page.url, "the token must not stay in the address bar"
    assert page.evaluate("sessionStorage.getItem('bmail.token')")
    assert "messages read" in page.inner_text("#rail-meta")
    assert not errors
    page.close()


def test_the_headline_states_the_true_number(page_factory):
    """The headline is a claim about reality, so it must not be the page size."""
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.window','3650')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    headline = page.inner_text(".headline")
    badge = page.inner_text('#nav a[data-tab="today"]')
    numbers = [int(n) for n in re.findall(r"\d+", headline)]
    if numbers:
        assert str(numbers[0]) in badge, "headline and sidebar disagree about the count"
    assert not errors
    page.close()


def test_every_extracted_fact_shows_its_source(page_factory):
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.window','3650')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    assert page.locator(".quote").count() > 0, "the evidence quote is the trust device"
    assert not errors
    page.close()


def test_an_empty_brief_offers_a_way_forward(page_factory):
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.window','1')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    if page.locator(".bar-row button").count():
        assert page.inner_text(".headline"), "an empty day still needs a sentence"
    assert not errors
    page.close()


def test_the_sidebar_speaks_the_packs_language(page_factory):
    """job-seeker tracks applications, freelance-ops tracks invoices."""
    page, errors, _config = page_factory()
    labels = " ".join(page.locator("#nav a").all_inner_texts()).lower()
    assert "application" in labels
    assert not errors
    page.close()


def test_the_stage_rail_only_fills_stages_that_happened(page_factory):
    """Inferring progress from the current state would claim interviews that
    never took place."""
    page, errors, _config = page_factory()
    page.click('[data-tab="cases"]')
    page.wait_for_selector(".stages")
    filled = page.evaluate("""() => {
      const card = document.querySelector('.card .stages');
      const done = card.querySelectorAll('.stage[data-done="1"]').length;
      const now = card.querySelectorAll('.stage[data-now="1"]').length;
      return { done, now, total: card.querySelectorAll('.stage').length };
    }""")
    assert filled["total"] > filled["done"], "a rail with everything filled says nothing"
    assert not errors
    page.close()


def test_cases_open_a_timeline(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="cases"]')
    page.wait_for_selector(".card")
    page.locator(".card").first.click()
    page.wait_for_selector(".timeline li")
    assert page.locator(".timeline li").count() >= 1
    assert not errors
    page.close()


def test_closed_cases_do_not_show_a_deadline(page_factory):
    """A rejected application has no outstanding task."""
    page, errors, _config = page_factory()
    page.click('[data-tab="cases"]')
    page.wait_for_selector(".card")
    stale = page.locator('.stack.closed .card .due-words[data-tone="urgent"]').count()
    assert stale == 0
    assert not errors
    page.close()


def test_mail_rows_do_not_collide(page_factory):
    """Subject and preview are separate lines, not overlapping text."""
    page, errors, _config = page_factory()
    page.click('[data-tab="mail"]')
    page.wait_for_selector('[data-list="mail"] .row')
    overlap = page.evaluate("""() => {
      const row = document.querySelector('.row');
      const t = row.querySelector('.row-title').getBoundingClientRect();
      const s = row.querySelector('.row-sub').getBoundingClientRect();
      return t.bottom > s.top + 1;
    }""")
    assert overlap is False
    assert not errors
    page.close()


def test_a_message_that_loads_slowly_still_opens(page_factory):
    """Opening a message redraws only the reading panes. When the daemon took
    longer than the placeholder waits, the placeholder was painted over the
    whole mailbox and the message arrived in a pane no longer on the page:
    grey bars, for good, until you left the screen."""
    page, errors, _config = page_factory()
    page.click('#nav a[data-tab="mail"]')
    page.wait_for_selector('[data-list="mail"] .row')

    def late(route):
        time.sleep(0.4)          # well past the 120ms the placeholder waits
        route.continue_()

    page.route("**/api/messages/*", late)
    page.locator('[data-list="mail"] .row').first.click()
    page.wait_for_selector(".context .sureness", timeout=5000)
    assert page.locator(".skel").count() == 0
    assert page.locator('[data-list="mail"] .row').count() > 0, "the list was thrown away"
    assert not errors
    page.close()


def test_mail_search_narrows_the_list(page_factory):
    """Scoped to the mail list: Today's "gone quiet" block also uses `.row`, and
    selecting on the bare class could measure whichever view was still on
    screen."""
    page, errors, _config = page_factory()
    page.click('[data-tab="mail"]')
    mail_rows = page.locator('[data-list="mail"] .row')
    page.wait_for_selector('[data-list="mail"] .row')
    before = mail_rows.count()
    assert before > 0
    page.fill('input[type="search"]', "Bewerbung")
    page.press('input[type="search"]', "Enter")
    page.wait_for_function(
        "n => document.querySelectorAll('[data-list=\"mail\"] .row').length !== n",
        arg=before, timeout=10000)
    assert mail_rows.count() <= before
    assert not errors
    page.close()


def test_a_correction_round_trips_to_the_store(page_factory):
    page, errors, config = page_factory()
    page.click('[data-tab="mail"]')
    page.wait_for_selector('[data-list="mail"] .row')
    page.locator('[data-list="mail"] .row').first.click()
    page.wait_for_selector(".aside select")
    message_id = page.evaluate(
        "new URLSearchParams(location.hash.split('?')[1]).get('id')"
    )
    page.select_option(".aside select", "personal")
    page.click(".aside button:has-text('Save')")
    page.wait_for_selector("#toast:not([hidden])")

    conn = connect(config.database)
    try:
        verdict = repo.get_verdict(conn, message_id, "job-seeker")
        assert verdict is not None
        assert verdict.category == "personal"
        assert verdict.stage.value == "manual"
    finally:
        conn.close()
    assert not errors
    page.close()


def test_review_asks_one_question_at_a_time(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="review"]')
    page.wait_for_timeout(900)
    if "Nothing to check" in page.inner_text(".headline"):
        page.close()
        return
    assert page.locator(".guess").count() == 1, "review shows one message, not a table"
    assert page.locator("button:has-text(\"Yes, that's right\")").count() == 1
    # Every alternative is offered: truncating hides the answer that matters.
    offered = page.locator(".choices button").count()
    total = page.evaluate("App.state.categories.length")
    assert offered == total - 1
    assert not errors
    page.close()


def test_a_review_answer_advances_the_queue(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="review"]')
    page.wait_for_timeout(900)
    if "Nothing to check" in page.inner_text(".headline"):
        page.close()
        return
    before = page.inner_text(".review-progress")
    page.click("button:has-text(\"Yes, that's right\")")
    page.wait_for_timeout(1200)
    after = page.inner_text(".review-progress") if page.locator(".review-progress").count() else ""
    assert after != before
    assert not errors
    page.close()


def test_a_sync_job_runs_from_the_sidebar(page_factory):
    page, errors, _config = page_factory()
    page.click("#btn-sync")
    page.wait_for_selector("#toast:not([hidden])", timeout=60000)
    assert page.inner_text("#toast")
    assert not errors
    page.close()


def test_a_second_screen_arriving_mid_fade_is_not_an_error(page_factory):
    """Screens cross-fade in. When the next one arrives before the fade has
    begun, the browser skips that fade and rejects its promise. Left unhandled,
    that was an error on the page for doing nothing wrong; it turned up when a
    screen followed the one before it within a frame."""
    page, errors, _config = page_factory()
    # Start a transition of our own the moment the app's has swapped the page
    # in, which is as close behind it as anything can arrive.
    page.evaluate("""() => new Promise((done) => {
      new MutationObserver((_changes, observer) => {
        observer.disconnect();
        document.startViewTransition(() => {}).finished.then(done);
      }).observe(document.querySelector("#main"), { childList: true });
      location.hash = "#/cases";
    })""")
    page.wait_for_timeout(300)
    assert page.locator("#main .page").count() == 1, "the skipped fade still swaps the page in"
    assert not errors
    page.close()


def test_a_slow_screen_does_not_draw_over_the_one_asked_for_after_it(page_factory):
    """Each screen loads, then draws. One asked for first and answered last
    drew over the one asked for since: the address said Setup and the page
    showed Today, with nothing to say so, until something else redrew it."""
    page, errors, _config = page_factory()
    page.evaluate("location.hash = '#/cases'")
    page.wait_for_selector("#main .page:not(.skel)")

    def late(route):
        time.sleep(0.5)
        route.continue_()

    page.route("**/api/brief*", late)
    with page.expect_response("**/api/brief*"):
        page.evaluate("location.hash = '#/today'")
        page.evaluate("location.hash = '#/setup'")
        page.wait_for_selector("text=Add a mailbox")
    page.wait_for_timeout(400)   # Today has its answer now, and has drawn if it is going to
    assert page.locator("#main .hello").count() == 0, "Today drew over Setup"
    assert page.locator("text=Add a mailbox").count() == 1
    assert not errors
    page.close()


def test_keyboard_navigation(page_factory):
    page, errors, _config = page_factory()
    page.keyboard.press("2")
    page.wait_for_timeout(400)
    assert page.url.endswith("#/cases")
    page.keyboard.press("5")
    page.wait_for_timeout(400)
    assert "setup" in page.url
    assert not errors
    page.close()


def test_it_reads_in_dark_mode(page_factory):
    """Both themes are first-class; a washed-out dark mode is a broken one."""
    page, errors, _config = page_factory(color_scheme="dark")
    contrast = page.evaluate(r"""() => {
      const s = getComputedStyle(document.body);
      const p = (c) => c.match(/\d+/g).slice(0, 3).map(Number);
      const lum = (c) => p(c).reduce((a, v) => a + v, 0) / 3;
      return { bg: lum(s.backgroundColor), fg: lum(s.color) };
    }""")
    assert contrast["bg"] < 80, "dark mode should actually be dark"
    assert contrast["fg"] - contrast["bg"] > 100, "text must stay readable"
    assert not errors
    page.close()


def test_no_external_requests_are_made(page_factory, ui):
    """A local-first tool must not phone anywhere, including for a font."""
    base, _app, _config = ui
    page, errors, _ = page_factory()
    external: list[str] = []
    page.on("request", lambda r: external.append(r.url)
            if not r.url.startswith(base) and not r.url.startswith("data:") else None)
    for tab in ("today", "cases", "mail", "review", "setup"):
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_timeout(400)
    assert external == [], f"the UI reached outside the machine: {external}"
    assert not errors
    page.close()


def test_switching_pack_switches_the_data_not_just_the_labels(page_factory):
    """The sidebar once relabelled itself for the new domain while every list
    underneath kept showing the old one's data — a working-looking switcher."""
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.pack','student-admissions')")
    page.goto(page.url.split("#")[0] + "#/cases")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    page.wait_for_timeout(600)

    nav = " ".join(page.locator("#nav a").all_inner_texts()).lower()
    assert "programme" in nav, "the sidebar should name the new pack's entity"

    options = " ".join(page.locator(".bar-row select option").all_inner_texts()).lower()
    assert "enquiry" in options, "the stage filter still lists the old pack's states"
    assert "acknowledged" not in options

    calls = page.evaluate("""async () => {
      const r = await fetch('/api/cases', {headers:{Authorization:'Bearer '+App.token}});
      return App.packChoice;
    }""")
    assert calls == "student-admissions"
    assert not errors
    page.close()


def test_mail_content_can_never_become_markup(page_factory, ui):
    """Mail is attacker-controlled text rendered in a privileged local page.

    The el() helper refuses innerHTML outright, so this asserts the outcome:
    a subject full of markup renders as visible text and adds no elements.
    """
    _base, _app, config = ui
    from datetime import datetime, timezone

    from bmail.models import EmailAddress, Message

    conn = connect(config.database)
    try:
        repo.bulk_upsert(conn, [Message(
            message_id="<xss@x>",
            date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            subject="<script>alert(1)</script> <img src=x onerror=alert(2)>",
            body_text="<b>bold</b> & <i>italic</i>",
            **{"from": EmailAddress(name="<i>Name</i>", address="x@example.com")},
        )])
    finally:
        conn.close()

    page, errors, _ = page_factory()
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(page.url.split("#")[0] + "#/mail")
    page.wait_for_selector('[data-list="mail"] .row')
    page.fill('input[type="search"]', "script")
    page.press('input[type="search"]', "Enter")
    page.wait_for_timeout(800)

    found = page.evaluate("""() => ({
      scripts: [...document.querySelectorAll('script')].map(s => s.src),
      imgs: document.querySelectorAll('img').length,
      asText: [...document.querySelectorAll('.row-title')].some(e => e.textContent.includes('<script>')),
    })""")
    assert dialogs == [], "mail content executed"
    assert found["imgs"] == 0, "mail content added an element"
    # Only the application's own scripts, named exactly: anything else on the
    # page came from somewhere it should not have.
    own = {"/app.js", "/icons.js"}
    assert all(any(src.endswith(name) for name in own) for src in found["scripts"] if src)
    assert len([src for src in found["scripts"] if src]) == len(own)
    assert found["asText"], "the subject should be visible as text"
    assert not errors
    page.close()


def test_long_subjects_do_not_break_the_layout(page_factory, ui):
    _base, _app, config = ui
    from datetime import datetime, timezone

    from bmail.models import EmailAddress, Message

    conn = connect(config.database)
    try:
        repo.bulk_upsert(conn, [Message(
            message_id="<long@x>",
            date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            subject="Betreff " + "einsehrlangeswortohneleerzeichen" * 10,
            body_text="A" * 3000,
            **{"from": EmailAddress(name="N" * 100, address="long" * 12 + "@example.com")},
        )])
    finally:
        conn.close()

    page, errors, _ = page_factory()
    page.goto(page.url.split("#")[0] + "#/mail")
    page.wait_for_selector('[data-list="mail"] .row')
    page.wait_for_timeout(500)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth > window.innerWidth + 2")
    assert overflow is False, "a long subject pushed the page sideways"
    assert not errors
    page.close()


def test_it_fits_a_narrow_window(page_factory):
    """The rail collapses to a top bar; nothing should scroll sideways."""
    page, errors, _config = page_factory(viewport={"width": 390, "height": 844})
    assert page.locator("#nav a").first.is_visible()
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth + 2")
    assert not errors
    page.close()


def test_the_page_never_violates_its_own_csp(page_factory):
    """The daemon serves a strict policy; the page has to live inside it.

    `style-src 'self'` refuses style attributes silently — the element simply
    renders unstyled — so the only way this is ever caught is by watching the
    console.
    """
    page, errors, _config = page_factory()
    for tab in ("today", "cases", "mail", "review", "setup"):
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_timeout(500)
    page.keyboard.press("Control+k")
    page.wait_for_selector(".sheet")
    page.wait_for_timeout(400)
    page.keyboard.press("Escape")
    page.keyboard.press("?")
    page.wait_for_timeout(400)
    violations = [e for e in errors if "Content Security Policy" in e]
    assert violations == [], f"the page broke its own CSP: {violations}"
    assert not errors
    page.close()


def test_progress_bars_actually_draw(page_factory):
    """Both bars are spans, and width does not apply to an inline box.

    They looked right in a screenshot because an empty 2px track and a track
    with no fill are the same picture.
    """
    page, errors, _config = page_factory()
    page.click('[data-tab="review"]')
    page.wait_for_timeout(900)
    if "Nothing to check" in page.inner_text(".headline"):
        page.close()
        return
    size = page.evaluate("""() => {
      const fill = document.querySelector('.review-progress .fill');
      const track = document.querySelector('.review-progress .track');
      return { fill: fill.getBoundingClientRect().width,
               track: track.getBoundingClientRect().width };
    }""")
    assert size["track"] > 0, "the track has no width"
    assert size["fill"] > 0, "the fill drew nothing"
    assert size["fill"] < size["track"], "a queue of one cannot be fully done"
    assert not errors
    page.close()


def test_the_typeface_is_served_locally_and_used(page_factory):
    """Inter ships in the package. If it silently failed to load, the page would
    fall back to system fonts and look fine in a screenshot — so check that it
    was fetched from this origin, with the right type, and is what renders."""
    page, errors, _config = page_factory()
    fonts: list[tuple[int, str]] = []
    page.on("response", lambda r: fonts.append((r.status, r.headers.get("content-type", "")))
            if r.url.endswith(".woff2") else None)
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    assert fonts and all(status == 200 and "woff2" in kind for status, kind in fonts), fonts
    assert page.evaluate("document.fonts.check('13px Inter')")
    # Browsers differ on whether they quote a single-word family when serialising.
    family = page.evaluate("getComputedStyle(document.body).fontFamily")
    assert family.split(",")[0].strip().strip('"') == "Inter", family
    assert not errors
    page.close()


def test_the_nav_indicator_starts_on_the_current_item(page_factory):
    """The indicator is first placed while the shell is still hidden, when every
    item measures zero; it has to be placed again once there is a layout."""
    page, errors, _config = page_factory()
    placed = page.evaluate("""() => {
      const pill = document.querySelector('.nav-pill');
      const item = document.querySelector('#nav a[aria-current="page"]');
      return { pill: pill.style.transform, height: pill.style.height,
               want: `translate(${item.offsetLeft}px, ${item.offsetTop}px)`,
               wantHeight: `${item.offsetHeight}px` };
    }""")
    assert placed["pill"] == placed["want"], placed
    assert placed["height"] == placed["wantHeight"], placed
    page.click('[data-tab="mail"]')
    page.wait_for_timeout(700)
    moved = page.evaluate("""() => document.querySelector('.nav-pill').style.transform
      === `translate(${document.querySelector('[data-tab="mail"]').offsetLeft}px, `
        + `${document.querySelector('[data-tab="mail"]').offsetTop}px)`""")
    assert moved, "the indicator did not follow the section"
    assert not errors
    page.close()


def test_a_long_html_mail_reads_in_full_in_the_message_pane(page_factory, ui):
    _base, _app, config = ui
    from email.mime.text import MIMEText

    from bmail.ingest.raw import parse_message

    paragraphs = "".join(
        f"<p>Point {i}: the team reviewed your profile and would like to go through it together.</p>"
        for i in range(40))
    html = (
        '<div style="display:none">Preview padding &zwnj;&nbsp;&zwnj;&nbsp;</div>'
        "<p>Hi Bhargav,</p>" + paragraphs +
        "<ul><li>your ID</li><li>a copy of your CV</li></ul>"
        "<p>Kind regards,<br>Jana</p>"
        "<p>On Mon, 21 Sep 2026 at 09:00, Bhargav &lt;b@example.com&gt; wrote:</p>"
        "<blockquote>My earlier question about the process.</blockquote>")
    mime = MIMEText(html, "html", "utf-8")
    mime["Subject"] = "Your interview: everything you need"
    mime["From"] = "Jana <jana@acme.example>"
    mime["Message-ID"] = "<longhtml@acme.example>"
    mime["Date"] = "Tue, 22 Sep 2026 10:00:00 +0200"
    conn = connect(config.database)
    try:
        repo.bulk_upsert(conn, [parse_message(mime.as_bytes(), account="demo")])
    finally:
        conn.close()

    page, errors, _ = page_factory()
    page.goto(page.url.split("#")[0] + "#/mail?id=" + "%3Clonghtml%40acme.example%3E")
    body = page.wait_for_selector(".reader .body-text")
    text = body.inner_text()
    assert "Point 39" in text and "• a copy of your CV" in text
    assert "Preview padding" not in text
    assert "My earlier question" not in text, "the quoted thread is set aside at first"
    boxed = page.evaluate("(el) => el.scrollHeight > el.clientHeight + 1", body)
    assert boxed is False, "the message is cut off inside a scrolling box"

    page.click("text=Show the whole message")
    assert "My earlier question" in body.inner_text()
    page.click("text=Show only the new part")
    assert "My earlier question" not in body.inner_text()
    assert not errors
    page.close()


def test_light_and_dark_can_be_chosen_and_apply_at_once(page_factory):
    """The choice beats the system in both directions, and survives a restart."""
    page, errors, _config = page_factory(color_scheme="light")
    background = "getComputedStyle(document.body).backgroundColor"
    light = page.evaluate(background)
    page.click('[data-tab="setup"]')
    picker = page.locator('.segmented[aria-label="Colours"]')
    picker.wait_for()
    assert picker.locator('[aria-pressed="true"]').inner_text() == "Match system"

    picker.locator('button[data-value="dark"]').click()
    assert page.evaluate("document.documentElement.dataset.scheme") == "dark"
    dark = page.evaluate(background)
    assert dark != light
    assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
    assert picker.locator('[aria-pressed="true"]').inner_text() == "Dark"

    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    assert page.evaluate(background) == dark, "the choice was forgotten"

    # A dark system does not override a light choice.
    page.emulate_media(color_scheme="dark")
    page.click('[data-tab="setup"]')
    page.locator('.segmented[aria-label="Colours"] button[data-value="light"]').click()
    assert page.evaluate(background) == light

    page.locator('.segmented[aria-label="Colours"] button[data-value="system"]').click()
    assert page.evaluate("document.documentElement.hasAttribute('data-scheme')") is False
    assert page.evaluate(background) == dark, "Match system should follow the dark system"
    assert not errors
    page.close()


def test_setup_says_which_version_this_is_and_where_its_update_stands(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="setup"]')
    about = page.locator("#about-bmail")
    about.wait_for()
    version = page.evaluate("App.state.version")
    assert f"Bmail {version}" in about.inner_text()
    assert "git pull" in about.inner_text(), "a browser tab has no shell to update it"

    # The desktop shell reports an update it has downloaded.
    page.evaluate("""bmailShell({version: '0.2.0', updates: 'ready', available: '0.2.1',
                                 notes: 'Faster start.'})""")
    about = page.locator("#about-bmail")
    assert "Bmail 0.2.1 is ready to install" in about.inner_text()
    assert about.locator("button.primary").inner_text() == "Restart now"
    assert "Faster start." in about.locator("details").inner_text() or about.locator("details").count() == 1
    assert "0.2.1 is ready" in page.inner_text("#toast")
    assert page.locator("#toast .toast-action").inner_text() == "Restart now"

    # Offline: it says so, and offers to try again.
    page.evaluate("bmailShell({version: '0.2.0', updates: 'error'})")
    assert "Try again" in page.locator("#about-bmail").inner_text()
    assert not errors
    page.close()
