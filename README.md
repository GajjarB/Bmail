<p align="center">
  <img src="media/logo.svg" width="72" height="72" alt="">
</p>

<h1 align="center">Bmail</h1>

<p align="center">
  <b>Your inbox, read on your own machine and turned into a tracker you never type into.</b><br>
  A local-first email triage engine for job hunts, university applications, hiring pipelines and invoices.
</p>

<p align="center">
  <a href="https://github.com/GajjarB/Bmail/actions/workflows/tests.yml"><img src="https://github.com/GajjarB/Bmail/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-10111A" alt="Python 3.10 to 3.13">
  <img src="https://img.shields.io/badge/licence-MIT-10111A" alt="MIT licence">
  <img src="https://img.shields.io/badge/runs-100%25%20local-1B6644" alt="Runs entirely locally">
  <img src="https://img.shields.io/badge/telemetry-none-1B6644" alt="No telemetry">
</p>

<p align="center">
  <a href="#see-it">See it</a> ·
  <a href="#download">Download</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#connect-your-mail">Connect your mail</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/">Docs</a>
</p>

---

## See it

<!--
  A real video player, with sound and controls:
  edit this file on github.com, drag media/bmail-film-short.mp4 onto the empty
  line below, and commit. GitHub puts a https://github.com/user-attachments/...
  link there, which it plays inline. Videos committed to the repository cannot
  be played in a README; only uploaded ones can.
-->


<a href="media/bmail-film-short.mp4">
  <img src="media/film.webp" alt="Bmail reading a deadline out of an email, putting it on a calendar, tracking the application and showing why it sorted a message the way it did" width="100%">
</a>

<p align="center">
  <sub>
    Under forty seconds, looping. <b><a href="media/bmail-film-short.mp4">Watch with sound</a></b> ·
    <a href="media/bmail-film.mp4">the full 1:50 tour</a>, with the board, the calendar and dark mode<br>
    Every frame is the running app on the sample corpus, with no mockups. The films are
    produced by a script, so they are remade whenever the interface changes (<a href="tools/">tools/</a>).
  </sub>
</p>

## What it does

| | |
|---|---|
| **Finds what needs you** | Interview invitations, assessments with deadlines, missing documents: surfaced first, each with the exact sentence that proves it, and kept there until it is dealt with. |
| **Tracks every case** | Each application, candidate or invoice gets a state machine. Two to six emails per case, out of order, in two languages, resolved into one row. |
| **Notices silence** | *"Helios Robotics: silent 34 days."* Ghosting detection tells you where one follow-up is worth sending. |
| **Stays current** | While the app is open, IMAP mail arrives within seconds (IDLE push); Gmail and Outlook are checked every few minutes. |
| **Never leaves your laptop** | No server, no account, no telemetry. The model runs locally, or not at all. |
| **Shows its working** | Every extracted date carries its source sentence, checked against the email. A date the email doesn't contain is dropped, not shown. |

The same brief in the terminal, on the sample mailbox (`bmail brief --limit 3`,
first half):

```
Bmail brief · Wed 23 Sep 2026 16:01 UTC · pack: job-seeker
========================================================================
4 need you · 2 deadlines · 1 upcoming · 8 stalled · 1 archived

NEEDS YOU
 1. Your application to MSc Computer Science
    Rug Example · MSc Computer Science · document request · Your application for MSc Computer Science is incomplete.
    deadline 2026-09-14
    evidence: "Please upload your certified transcript and language certificate no later than 14 September 2026."
 2. Ihre Bewerbung: MSc Data Science
    Tu Rheinland Example · MSc Data Science · document request · Ihre Bewerbung für MSc Data Science ist unvollständig.
    deadline 2026-09-04
    evidence: "Bitte reichen Sie noch die beglaubigte Übersetzung Ihrer Zeugnisse bis zum 04.09.2026 nach."
 3. Interview invitation – Platform Engineer
    Cobalt Health · Platform Engineer · interview invite · We would like to invite you to an interview for the Platform Engineer position.
    Wed 07 Oct 09:00 UTC
    evidence: "We propose 07 October 2026 at 10:00 CET."

DEADLINES
 · Ihre Bewerbung: MSc Data Science · overdue by 19 d (2026-09-04)
 · Your application to MSc Computer Science · overdue by 9 d (2026-09-14)
```

## Why this exists

"Too many emails" is three problems, and most tools solve one:

| Problem | What it actually is | Solved by |
|---|---|---|
| **Volume:** 100+ a day, most irrelevant | A filtering problem | Rules plus a small classifier. Cheap. |
| **Comprehension:** long threads, buried asks | A summarisation problem | A language model. |
| **State:** *"did that company ever reply?"* | A **stateful tracking** problem | Entity resolution and a state machine, not summarisation. |

Nobody solves the third one well. Track 90 roles and each generates two to six
emails: acknowledgement, portal invite, assessment link, scheduling,
rejection. That is 200–500 messages of pure state change, arriving out of
order, from applicant-tracking systems that all write differently. No human
holds that, and a spreadsheet holds only what you remember to type in. That is
the gap Bmail fills.

### Why local is the design, not a feature

- **No recurring compliance cost.** Gmail's `readonly` and `modify` scopes are
  *restricted*: an app that stores or transmits that data on servers needs an
  annual third-party security assessment. A desktop app that never ships mail
  content anywhere sidesteps it entirely.
- **GDPR.** Your inbox holds personal data about *other people*: recruiters,
  professors, candidates. Process it locally and, for personal use, there is no
  processor at all.
- **Unit economics.** Backfilling three years of mail is hundreds of thousands
  of tokens, exactly the workload cloud APIs punish. Locally it is a warm
  laptop for an hour, once.

More in [docs/PRIVACY.md](docs/PRIVACY.md).

## Download

Installers for macOS (Apple silicon), Windows and Linux are on the
[Releases page](https://github.com/GajjarB/Bmail/releases). Open the app and it
walks you through connecting a mailbox. Nothing else to install: the engine is
inside. From 0.2.0 it keeps itself up to date: new versions download in the
background and install when you close it. What changed in each version:
[CHANGELOG.md](CHANGELOG.md).

Until the builds are signed, your system will warn you the first time. On
macOS: System Settings → Privacy & Security → Open Anyway. On Windows: More info
→ Run anyway. How releases are made: [docs/RELEASING.md](docs/RELEASING.md).

## Quick start

To run it from source instead:

Python 3.10 or newer. The core needs four packages.

```bash
git clone https://github.com/GajjarB/Bmail.git
cd Bmail
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then open the app:

```bash
bmail ui                      # opens in your browser, on a loopback address
```

The first run walks you through it: pick what to track (job applications,
university applications, candidates or invoices), connect a mailbox, and watch
it read. No mailbox handy? **Try it with sample mail**, a made-up English and
German job search, from the same screen.

Prefer the terminal? The same, with no credentials:

```bash
bmail init
bmail samples --out ./sample
bmail ingest --path ./sample/sample.mbox --account demo
bmail triage
bmail brief                   # and `bmail cases` for the tracker
```

<details>
<summary><b>Optional extras</b></summary>

```bash
pip install -e '.[gmail]'      # Gmail
pip install -e '.[graph]'      # Outlook and Microsoft 365
pip install -e '.[ml]'         # the trainable classifier
pip install -e '.[html]'       # faster HTML extraction
pip install -e '.[embeddings]'
pip install -e '.[all]'
```

For Stage 3, install [Ollama](https://ollama.com) and pull the model
`bmail doctor` suggests for your hardware. **Everything except Stage 3 works
without it.** Bmail falls back to rules plus the classifier and says so.

</details>

## Connect your mail

In the app, **Setup → Add a mailbox** (or the first-run screen): type your
address, and Bmail recognises Gmail, GMX, WEB.DE, Yahoo, iCloud, T-Online,
Posteo and mailbox.org, fills in the server, and says what that provider needs,
usually an app password. It signs in before saving anything, keeps the password
in your system keychain, and reads the last six months by default. Any other
IMAP server works too; give its address under *Server settings*.

A file (a Gmail Takeout export or any `.mbox`) needs no sign-in at all. Accounts
can also be written into `config.toml` by hand, which is how Gmail's own API and
Outlook are connected today:

| Source | How it stays current | Setup |
|---|---|---|
| **File:** mbox, Maildir, `.eml` | Read once, on demand | None |
| **IMAP:** Gmail, GMX, WEB.DE, Yahoo, iCloud, university mail | **Live**: the server announces new mail (IDLE) and it is sorted within seconds | In the app: address and app password, kept in the OS keychain |
| **Gmail API** | Checked on an interval, 5 minutes by default | A free Google Cloud OAuth client, in `config.toml` |
| **Outlook / Microsoft 365** | Checked on an interval | A free Entra app registration, in `config.toml`; Microsoft no longer accepts app passwords |

Each check fetches and sorts in one go. Background checks never open a sign-in
window: if a token expires, the account is marked *needs you to sign in* and you
do it from the button. Change the interval, or turn it off, under
**Setup → Check automatically**. Step-by-step for each provider:
[docs/PROVIDERS.md](docs/PROVIDERS.md).

```bash
bmail ingest --path ~/Downloads/takeout/Mail/All\ mail.mbox --account archive
bmail accounts --set-password uni     # IMAP password into the keychain
```

## The app

`bmail ui` serves the app from the local daemon and opens it. A native window
built on Tauri runs the same page:

```bash
cd desktop/src-tauri && cargo build --release && ./target/release/bmail-desktop
```

It is a briefing rather than a dashboard:

- **Today** opens with how many things are waiting on you, then the things
  themselves, each with its deadline and the quoted sentence behind it. A
  request stays until it is settled: you replied, the application moved on, or
  its date passed. Ten copies of one message are one thing to do.
- **Applications** draws every case's state machine as a rail, filled only as
  far as it actually got.
- **Mail** is everything, searchable.
- **Review** asks about one uncertain guess at a time; each answer teaches the
  classifier.

Two densities, **Studio** and **Compact**, light and dark. Fully keyboard
driven: `j`/`k` to move, `↵` to open, `esc` to close, `/` to search, `c` to
check for mail, `⌘K` for everything, `?` for the rest. Vanilla HTML, CSS and
JavaScript: no framework, no build step, and a test asserts it makes no
external requests. See [docs/UI.md](docs/UI.md).

## How it works

Four stages. Each may settle a message and stop; only what survives goes on.

```
Stage 1  headers, contact graph, ATS template recipes, near-duplicate collapse
           └─ resolves most of an inbox with zero model calls
Stage 2  local embeddings + a calibrated classifier
           └─ accept when confident; escalate when not, or when a miss is expensive
Stage 3  local LLM, schema-constrained, every fact carrying its source sentence
Stage 4  entity resolution + a per-pack state machine
```

**Deterministic first.** Applicant-tracking systems send the same text with
three fields swapped. Fingerprint the boilerplate once and extraction becomes a
regex: no model call, milliseconds, deterministic. `bmail templates` shows how
much of your own mail that covers; it is usually more than people expect.

**Asymmetric routing.** Missing an interview invitation is catastrophic; a false
alarm costs three seconds. High-stakes categories go to the model regardless of
confidence, and packs declare which those are.

**Every fact shows its source.** Each extracted date arrives with the sentence
it came from, and that sentence is checked against the email. This makes
hallucination visible and measurable instead of silent.

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Domain packs

Job seekers, university applicants and recruiters are the same machine with
different vocabulary. A pack is a YAML file declaring categories, a case key, a
state machine and KPIs; the engine is domain-agnostic.

| Pack | Tracks | Standout |
|---|---|---|
| `job-seeker` | applications | Ghosting detection, ATS parsing, response rates |
| `student-admissions` | programme applications | Deadlines from uni-assist, DAAD and portals; missing-document alerts |
| `hr-recruiter` | candidates × requisitions | Response-time SLAs, candidate dedupe |
| `freelance-ops` | invoices | Amounts and due dates, an overdue chase list |

Write your own: [docs/PACKS.md](docs/PACKS.md).

## Outputs

SQLite is the source of truth; a spreadsheet is an export.

```bash
bmail export --xlsx tracker.xlsx    # Overview / Cases / Timeline / Deadlines / Contacts
bmail export --ics deadlines.ics    # interviews as events, deadlines with alarms
bmail export --csv ./out
bmail notion --database <id>        # fill a Notion database you already have
bmail labels                        # plan provider labels; writes nothing until you confirm
```

## Evaluation

Built in from the start, because trust is the product.

```bash
bmail eval labels.jsonl --ablate
```

Per-class precision, recall and F1, with **recall on `action_required`** as the
headline number; precision@5 on the brief; field-level accuracy (exact on dates
including time zone, fuzzy on organisation names); a hallucination rate; and
latency, across rules-only, +classifier, +LLM and LLM-on-everything, for German
and English separately. See [docs/EVALUATION.md](docs/EVALUATION.md).

## Security

Email is attacker-controlled text being fed to a model, so prompt injection is
not hypothetical.

- **The model has no tools.** It emits JSON; every side effect happens in Python
  from validated fields.
- **The body is data, not instructions:** fenced, with the delimiters stripped
  from the content first.
- **Nothing is fetched.** URLs are reduced to their host before reaching a
  prompt: no tracking pixels, no SSRF.
- **Nothing is ever deleted, moved or sent.** The only outward write is a label,
  and only after you confirm it.
- **Passwords live in the OS keychain**, never the config file. Plain-text IMAP
  is refused except to a bridge on your own machine.
- **The local server is loopback-only**, behind a session token that is new
  at every start, with `Host` pinned against DNS rebinding and `Origin` checked
  on every write. It never outlives its window.
- **Updates are signed.** The desktop app installs an update only if it carries
  the project's signature, checked against a key built into the app.

Found a security problem? Please report it privately: see
[SECURITY.md](SECURITY.md).

## Commands

<details>
<summary><b>All commands</b></summary>

| Command | What it does |
|---|---|
| `bmail init` | Create the config and the store |
| `bmail doctor` | Check the environment, models and privacy settings |
| `bmail samples` | Generate a synthetic corpus with ground truth |
| `bmail accounts` | List accounts, store passwords in the keychain |
| `bmail ingest` | Pull mail from a file, IMAP, Gmail or Outlook |
| `bmail triage` | Run the pipeline |
| `bmail brief` | What needs you, in one screen (`--format md\|json`) |
| `bmail cases` | The tracker (`--id N` for a full timeline) |
| `bmail review` | Confirm low-confidence verdicts; each answer trains the classifier |
| `bmail search` | Full-text search |
| `bmail templates` | Recipe coverage over your own corpus |
| `bmail export` | Excel, CSV, calendar |
| `bmail notion` | Sync cases into an existing Notion database |
| `bmail labels` | Plan and apply provider labels |
| `bmail eval` | Score the pipeline; `--ablate` for the comparison |
| `bmail packs` | List and describe domain packs |
| `bmail ui` | Open the app |
| `bmail serve` | Run the local daemon without opening a browser |

</details>

<details>
<summary><b>Use it as a library</b></summary>

The core is importable and headless; the CLI is a client of it:

```python
from bmail.store.db import connect
from bmail.packs import load_pack
from bmail.classify import Classifier
from bmail.pipeline import Pipeline
from bmail.store import repo

conn = connect("~/.local/share/bmail/bmail.sqlite3")
pack = load_pack("job-seeker")
stats = Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(
    conn, repo.iter_messages(conn, limit=200)
)
```

</details>

## Status

**Working today:** file, mbox, Maildir, IMAP, Gmail and Microsoft Graph ingest;
background sync while the app is open (IMAP push, interval polling); all four
stages; four packs; 57 starter template recipes; the brief, cases and ghosting
detection; Excel, CSV, ICS and Notion export; Gmail label and Outlook category
write-back; the evaluation harness and ablations; the app in the browser and in
a native window.

**Not built yet:** `sqlite-vec` embedding storage, and signed installers:
signing needs per-developer Apple and Windows certificates that cannot live in a
repository.

**Not planned: compose and send.** The moment Bmail becomes a mail client the
project doubles: rendering, offline sync, threading, MIME edge cases. Bmail
sits *beside* your mail client and writes back only labels and categories.

## Development

```bash
pip install -e '.[dev,ml,html]'
pytest -q
ruff check src tests
```

The connectors are tested against a real IMAP server on a local socket and a
fake Gmail API; the interface is tested in real Chromium through Playwright
(skipped when it is not installed; `pip install playwright && playwright
install chromium` to run them). CI runs the suite on Python 3.10 to 3.13 and the
browser tests on every push. The native shell needs Rust, and WebKitGTK
on Linux: `cd desktop/src-tauri && cargo build --release`.

## Licence

MIT. See [LICENSE](LICENSE).

The interface bundles two third-party assets, both free to use and redistribute,
each shipped with its licence beside it: the **[Inter](https://rsms.me/inter/)**
typeface by Rasmus Andersson (SIL Open Font License 1.1) and
**[Lucide](https://lucide.dev)** icons (ISC). Both are served from your machine;
the app fetches nothing from the network.
