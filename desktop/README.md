# Bmail desktop shell

A Tauri v2 window around the local Bmail daemon. It owns a window and a child
process; everything else lives in the Python core.

```bash
cd src-tauri
cargo build --release
./target/release/bmail-desktop
```

Requires Rust, and on Linux the WebKitGTK development packages:

```bash
sudo apt install libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev build-essential
```

To build installers with the engine inside, freeze the engine first, then
bundle:

```bash
python ../packaging/build_sidecar.py      # writes the engine folder, src-tauri/engine/
npm ci && npx tauri build                 # installers under src-tauri/target/release/bundle/
```

Releases do the same on GitHub Actions for every tag; see
[../docs/RELEASING.md](../docs/RELEASING.md).

The window opens at once on `dist/index.html`, a start screen that looks like
the app's own, while the engine starts. The shell looks for it in this order:
`engine/bmail` in its resources (what the installer ships), a `bmail` binary
beside itself (older builds), `$BMAIL_BIN`, `bmail` on `PATH`, then
`python -m bmail`. It reads one JSON line from `bmail serve --json` to learn the
loopback URL and session token, then points the window at it. If nothing
starts, the start screen says why.

If you have no Rust toolchain, `bmail ui` gives you the same application in a
browser tab.

See [../docs/UI.md](../docs/UI.md).
