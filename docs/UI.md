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

This is a briefing, not a dashboard. Someone opens it in the morning anxious
about ninety applications and wants to be told two things: what needs them, and
what has gone quiet. So the interface talks in sentences — *"Eighteen things
need you."* — rather than lining up stat tiles, and it gives one thing at a time
real weight.

Three rules follow from that, and they are worth keeping:

1. **Numbers live inside sentences.** A row of counters is what you build when
   you have not decided what matters.
2. **Colour means exactly one thing.** Red is overdue, amber is this week, green
   is moving, slate is silent, grey is closed — the same everywhere, in a dot, a
   chip and a card edge.
3. **Nothing claims more than it knows.** The headline count is the real total,
   not the page size. The stage rail fills only the stages a case actually
   reached. An extracted date always shows the sentence it came from.

## The surfaces

- **Today** — the briefing. A headline that states the day's truth, then the
  things that need you, each with its deadline and the quoted sentence that
  proves it. Underneath: what is coming up, what has gone quiet, and what was
  filed away without bothering you.
- **Applications** — the tracker, named after whatever the active pack tracks:
  applications, programme applications, candidates or invoices. Each case draws
  its own state machine as a rail, live ones first and closed ones receding.
- **Mail** — everything Bmail has read, searchable and filterable, with a detail
  panel that shows what Bmail concluded and why.
- **Review** — the uncertain guesses, one at a time. A question, a big *yes*,
  and every alternative offered. Each answer becomes a training row, which is
  what makes the classifier personal within a week.
- **Setup** — collect mail, sort it, switch pack, export, and see where things
  live on disk.

Keyboard: `1`–`5` switch surface, `r` reloads, `c` checks for new mail, `s`
sorts it.

**Check for new mail** fetches from every account and sorts what arrived, in one
job. If one account fails — an expired sign-in, a server that is down — the
others still sync and the toast names the one that did not.

It also happens on its own while the app is open: on an interval set in Setup,
and within seconds for IMAP servers that push new mail. The line under the
button says when the last check was — a breathing dot means the server is
pushing — or which account needs attention. The page asks the engine every 15
seconds and redraws only when something new actually arrived. See
[PROVIDERS.md](PROVIDERS.md#staying-up-to-date).

Long jobs — ingest, triage, export, background checks — run on a single
background worker with a progress bar. One at a time, because the store has one
writer and two triage runs at once is never what anyone wanted.

## Architecture

```
  Tauri window  ─┐
                 ├──▶  http://127.0.0.1:<random>   ──▶  bmail.api  ──▶  SQLite
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
| Random port, session token required | Another local process cannot read your mail by guessing a port |
| `Host` header pinned to loopback | Blocks DNS rebinding, which is how a web page reaches a localhost server |
| `Origin` checked on every request | Blocks cross-site writes from a page you happen to have open |
| Strict `Content-Security-Policy` | The page loads nothing from anywhere else, and says so |

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

The shell finds the engine in this order: a `bmail` binary next to itself (a
bundled sidecar), `$BMAIL_BIN`, `bmail` on `PATH`, then `python -m bmail`. If none
of them starts, the window shows a page saying so rather than opening blank.

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
