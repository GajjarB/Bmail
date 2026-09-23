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
python ../packaging/build_sidecar.py      # writes src-tauri/binaries/bmail-<target>
npm ci && npx tauri build                 # installers under src-tauri/target/release/bundle/
```

Releases do the same on GitHub Actions for every tag; see
[../docs/RELEASING.md](../docs/RELEASING.md).

The shell looks for the engine in this order: a `bmail` binary beside itself (a
bundled sidecar), `$BMAIL_BIN`, `bmail` on `PATH`, then `python -m bmail`. It reads
one JSON line from `bmail serve --json` to learn the loopback URL and session
token, then points a webview at it. If nothing starts, the window shows
`dist/index.html`, which says so.

If you have no Rust toolchain, `bmail ui` gives you the same application in a
browser tab.

See [../docs/UI.md](../docs/UI.md).
