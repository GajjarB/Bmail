# Sift desktop shell

A Tauri v2 window around the local Sift daemon. It owns a window and a child
process; everything else lives in the Python core.

```bash
cd src-tauri
cargo build --release
./target/release/sift-desktop
```

Requires Rust, and on Linux the WebKitGTK development packages:

```bash
sudo apt install libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev build-essential
```

The shell looks for the engine in this order: a `sift` binary beside itself (a
bundled sidecar), `$SIFT_BIN`, `sift` on `PATH`, then `python -m sift`. It reads
one JSON line from `sift serve --json` to learn the loopback URL and session
token, then points a webview at it. If nothing starts, the window shows
`dist/index.html`, which says so.

If you have no Rust toolchain, `sift ui` gives you the same application in a
browser tab.

See [../docs/UI.md](../docs/UI.md).
