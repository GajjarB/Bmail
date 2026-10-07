# The desktop UI

Two ways to run the same application. Both serve the same page from the same
local daemon; the only difference is what holds the window.

```bash
bmail ui                 # opens your browser at a loopback address
```

```bash
cd desktop/src-tauri && cargo build --release
./target/release/bmail-desktop      # a native window
```

## The idea

Someone opens this in the morning anxious about ninety applications and wants
to be told two things: what needs them, and what has gone quiet. The design
does that the way the best current productivity apps do. The sidebar sits on a
warm grey ground, with a white pill marking where you are, and the work sits on
one white panel with a top bar: when mail was last checked, search, and the one
black button that checks for mail. Hairlines divide things, not shadows; there
are no gradients, no glows and nothing that lifts on hover.

Four rules are worth keeping:

1. **Chrome is grey; content gets the colour.** Every sender has an avatar in
   one of eight soft hues, stable for that sender. Every kind of mail is a
   plain tag with a small dot in its colour, the same on Today, in Mail and on
   the board. The primary button is black. Colour on the chrome would drown
   the colour that means something.
2. **Status colour means exactly one thing.** Red is overdue, amber is this
   week, green is moving or booked, grey is closed or silent, in the words
   under a card and on a calendar day alike.
3. **Nothing claims more than it knows.** The counts are real totals, not page
   sizes. The stage rail fills only the stages a case actually reached. An
   extracted date always shows the sentence it came from.
4. **Everyone can use it.** Every text colour clears 4.5:1 against its
   background in both themes, every control has a name a screen reader can
   say, and the drawer is a real modal dialog: focus moves into it, Tab stays
   in it, Escape closes it and focus returns to the card it came from. The
   pages are checked with axe-core, light and dark, and report nothing.

## The surfaces

- **Today**: a greeting by name (taken from your own sent mail), a sentence
  with the day's truth, and a strip of four numbers: what needs you, open
  applications (and how many closed), what is coming up in the next two weeks
  (and how many are already overdue), and how many guesses are waiting to be
  checked. Then the things that need you as cards, each with its kind, its
  deadline and the quoted sentence that proves it. Beside them is a month
  calendar with a dot on every day something is due. A marked day is a
  button that narrows the list under it to that day, and the arrows move
  between months only where there is something to see. Each dated thing links
  to its application, or to the mail it came from. At the foot, how much was
  filed for you, over a period you choose, with a link to that mail.
- **Applications**: a board, one column per stage in the pack's own order, so
  you see where each application is and where it goes next. A stage with
  nothing in it is a slim strip that takes no room. **All** or **Gone quiet**,
  and a stage menu, narrow it. Opening a card slides its history in from the
  right; closing it keeps the filters you had.
- **Mail**: three panes, like a mail client. Message cards on the left
  (sender, subject, two lines, its kind), with **All**, **Needs me** and
  **Filed**, and a menu of every kind; the message in the middle, subject
  first, then who sent it and when; and what Bmail made of it on the right:
  the kind, how sure it is and what decided it ("95% sure, recognised by a
  rule"), the sentence that says so, the application it belongs to, and a
  way to change it. Opening another message changes only the
  reading panes, so the list stays where you scrolled it.
- **Review**: the uncertain guesses, one at a time. A question, a big *yes*,
  and every alternative offered. Each answer becomes a training row, which is
  what makes the classifier personal within a week.
- **Setup**: add and remove mailboxes, choose how often to check, sort,
  switch what is tracked, export, choose light or dark colours and the layout,
  see where things live on disk, and, under About, which version this is and
  where its next update stands (with **Restart now** once one is ready).

Colours are **Match system**, **Light** or **Dark**, and change the moment you
pick one.

Nothing you set up is asked again. The engine keeps your settings (colours,
layout, the period "filed" counts) in its own store, not only in the browser,
and it serves the page from the same address every start (port 47113, or any
free port when another program holds that one). The welcome counts as done
the moment a mailbox is connected or mail starts being read; closed before
that, it carries on from the step it reached. Match system follows the
operating system's setting as it changes. The same switch is in the command
palette (`Ctrl+K`, "Switch to dark mode").

Two layouts share the markup: **Studio** (the default, described above) and
**Compact**, denser, for when ninety applications need to fit on a screen.

A new install opens on a four-step welcome instead of an empty Today: what to
track, where the mail is (a mailbox, an export file, or sample mail), reading
it, and what it found. It can be skipped, and everything it asks can be changed
later in Setup. The choice of what to track is kept by the engine, not the
browser, so background checks sort with it too.

Keyboard: `1`–`5` switch surface, `r` reloads, `c` checks for new mail, `s`
sorts it.

**Check for new mail** fetches from every account and sorts what arrived, in one
job. If one account fails — an expired sign-in, a server that is down — the
others still sync and the toast names the one that did not.

It also happens on its own while the app is open: on an interval set in Setup,
and within seconds for IMAP servers that push new mail. The line under the
button says when the last check was — a breathing dot means the server is
pushing — or which account needs attention. The page asks the engine every 15
seconds. Mail that arrived on its own never redraws what you are reading: the
counts in the sidebar change where they are, and the toast offers **Show**. An
empty screen (a first import) fills in by itself, since there is nothing to
lose. See
[PROVIDERS.md](PROVIDERS.md#staying-up-to-date).

Long jobs — ingest, triage, export, background checks — run on a single
background worker with a progress bar. One at a time, because the store has one
writer and two triage runs at once is never what anyone wanted.

## Architecture

```
  Tauri window  ─┐
                 ├──▶  http://127.0.0.1:47113      ──▶  bmail.api  ──▶  SQLite
  Browser tab   ─┘         (token-gated)                  │
                                                          └──▶ bmail.jobs (1 worker)
```

The shell owns a window and a child process and nothing else; all the logic is
in the Python core. That is deliberate — the core stays importable, scriptable
and testable on its own, and the UI is a client of it rather than a container
for it. It is also why `bmail ui` works identically with no Rust toolchain
anywhere.

The page is vanilla HTML, CSS and JavaScript with no build step, no bundler and
no framework — about 1,400 lines all told, of which roughly a third is
the design system. It is served from the machine it runs on and has to
work with the network unplugged, so there is nothing to fetch and nothing to
compile — the file you read is the file that runs. A test asserts the page makes
no external requests at all.

## Security

The local server reads mail, so it is guarded on five fronts:

| Guard | Why |
|---|---|
| Binds `127.0.0.1` only, never `0.0.0.0` | Nothing outside the machine can reach it |
| Session token required, new on every start | Another local process that finds the port still cannot read your mail |
| `Host` header pinned to loopback | Blocks DNS rebinding, which is how a web page reaches a localhost server |
| `Origin` checked on every request | Blocks cross-site writes from a page you happen to have open |
| Strict `Content-Security-Policy` | The page loads nothing from anywhere else, and says so |
| A message as sent is served sandboxed, under its own policy, at a one-time address | Someone else's HTML can be looked at and nothing more: no script, no form, no window, and no remote content but pictures over `https` |
| Links in a message are opened by the engine, in your browser | The frame may not open windows; only `http`, `https` and `mailto` addresses are accepted, and only on a click |

The token is passed once in the URL, moved into `sessionStorage`, and stripped
from the address bar so it does not end up in history or a screenshot.

Static files are resolved inside the asset directory only, so `..` cannot walk
out of it.

**The daemon never outlives its window.** The shell kills it when the window
closes, and — because that cannot cover being killed outright — the daemon also
watches its parent process and shuts itself down when that disappears. Both
paths are tested, including `SIGKILL`.

## Building the native shell

Requires Rust and, on Linux, WebKitGTK:

```bash
# Debian/Ubuntu
sudo apt install libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev build-essential

cd desktop/src-tauri
cargo build --release          # ./target/release/bmail-desktop
cargo tauri build              # installers, if the Tauri CLI is installed
```

macOS and Windows need no extra system packages beyond Xcode command-line tools
and the MSVC build tools respectively.

The window opens at once on a start screen and switches to the app when the
engine is ready. The shell finds the engine in this order: `engine/bmail` in the
app's resources, a `bmail` binary next to itself (older builds), `$BMAIL_BIN`,
`bmail` on `PATH`, then `python -m bmail`. If none of them starts, the start
screen says why rather than waiting for ever.

Installers are not signed. Signing needs an Apple Developer ID and a Windows
code-signing certificate, both of which are per-developer and cannot be
committed to a repository.

## Adding a view

1. Add an endpoint to `BmailApp` in `src/bmail/api.py` and route it in
   `_route_get`.
2. Add a `view*` function in `src/bmail/ui/static/app.js` and register it in
   `ROUTES`.
3. Add an entry to the `items` list in `paintNav()`.

Use the existing tokens rather than new colours, and pick a tone from
`toneFor()` rather than inventing another meaning for red.

There is no build step, so a reload is enough to see the change. Elements are
built with the `el()` helper, which refuses `innerHTML` outright — every value
that reaches the page goes through `textContent`, so mail content can never
become markup.
