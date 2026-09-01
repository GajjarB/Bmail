# The desktop UI

Two ways to run the same application. Both serve the same page from the same
local daemon; the only difference is what holds the window.

```bash
sift ui                 # opens your browser at a loopback address
```

```bash
cd desktop/src-tauri && cargo build --release
./target/release/sift-desktop      # a native window
```

## What it does

Four surfaces, matching the three the engine was designed around plus the
feedback loop that makes it improve:

- **Brief** — one screen: what needs you, ranked, with the deadline and the
  source sentence for every extracted fact. Deadlines, upcoming events, stalled
  cases and a collapsed archive list sit underneath.
- **Cases** — the tracker. One row per application, candidate or invoice, with
  its state, how long it has been silent, and a full message timeline when you
  open it.
- **Messages** — everything, filterable by category, "needs action" and
  full-text search, paged.
- **Review** — the low-confidence verdicts, and the point of the whole loop:
  correcting one writes a training row, so the classifier gets personal within
  a week.
- **Settings** — ingest, triage, export and the store, plus which pack is active.

Keyboard: `1`–`5` switch surface, `r` reloads, `t` starts a triage.

Long jobs — ingest, triage, export — run on a single background worker with a
progress bar. One at a time, because the store has one writer and two triage
runs at once is never what anyone wanted.

## Architecture

```
  Tauri window  ─┐
                 ├──▶  http://127.0.0.1:<random>   ──▶  sift.api  ──▶  SQLite
  Browser tab   ─┘         (token-gated)                  │
                                                          └──▶ sift.jobs (1 worker)
```

The shell owns a window and a child process and nothing else; all the logic is
in the Python core. That is deliberate — the core stays importable, scriptable
and testable on its own, and the UI is a client of it rather than a container
for it. It is also why `sift ui` works identically with no Rust toolchain
anywhere.

The page itself is vanilla HTML, CSS and JavaScript with no build step, no
bundler and no framework. It is served from the machine it runs on and has to
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
cargo build --release          # ./target/release/sift-desktop
cargo tauri build              # installers, if the Tauri CLI is installed
```

macOS and Windows need no extra system packages beyond Xcode command-line tools
and the MSVC build tools respectively.

The shell finds the engine in this order: a `sift` binary next to itself (a
bundled sidecar), `$SIFT_BIN`, `sift` on `PATH`, then `python -m sift`. If none
of them starts, the window shows a page saying so rather than opening blank.

Installers are not signed. Signing needs an Apple Developer ID and a Windows
code-signing certificate, both of which are per-developer and cannot be
committed to a repository.

## Adding a view

1. Add an endpoint to `SiftApp` in `src/sift/api.py` and route it in
   `_route_get`.
2. Add a `view*` function in `src/sift/ui/static/app.js` and a branch in
   `render()`.
3. Add a tab to `index.html`.

There is no build step, so a reload is enough to see the change. Elements are
built with the `el()` helper, which refuses `innerHTML` outright — every value
that reaches the page goes through `textContent`, so mail content can never
become markup.
