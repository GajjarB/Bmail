"""Freeze the Bmail engine, and prove it works.

The desktop shell ships the engine with itself, so installing Bmail does not
mean installing Python. This builds it with PyInstaller as a folder, not one
file, into desktop/src-tauri/engine/, which the installer copies as it is. One
file would have to unpack its whole Python into a temporary folder on every
launch, and on Windows the virus scanner reads every unpacked file again each
time: seconds of blank screen before the window could show anything. A folder
is unpacked once, when Bmail is installed.

Then it runs what it built: starts `bmail serve`, fetches the page, a script,
and the state API. A frozen app that is missing a data file still builds
cleanly and only fails on someone's machine, so the check is the point.

    python packaging/build_sidecar.py            # build for this machine, then test
    python packaging/build_sidecar.py --no-test  # build only
    python packaging/build_sidecar.py --test-only
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "desktop" / "src-tauri" / "engine"
WORK = ROOT / "build" / "sidecar"

# Big, optional, or never used by the engine: kept out so the download stays
# small. The trainable classifier (numpy, scikit-learn) and the Gmail API and
# Microsoft libraries are optional extras; the app connects over IMAP and falls
# back to the prototype classifier, and says so.
EXCLUDE = ["tkinter", "numpy", "sklearn", "scipy", "torch", "sentence_transformers",
           "googleapiclient", "google_auth_oauthlib", "msal", "matplotlib", "IPython",
           "pytest", "playwright",
           # selectolax loads its old Modest engine (LGPL-2.1) whenever it is
           # imported. Python's own parser gives the same text (the tests hold
           # the two to it) at about the same speed, so the app ships without
           # it and carries no copyleft code at all.
           "selectolax"]


def target_triple() -> str:
    """The Rust target this machine builds for, as Tauri names sidecars."""
    try:
        out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}[machine]
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform == "win32":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-unknown-linux-gnu"


def engine_path(triple: str) -> Path:
    return ENGINE / ("bmail.exe" if "windows" in triple else "bmail")


def icon_arguments(triple: str) -> list[str]:
    """The engine has no window, but Windows still draws its icon in Task
    Manager and in any prompt about it. Unset, that is the freezer's own."""
    if "windows" not in triple:
        return []
    return ["--icon", str(ENGINE.parent / "icons" / "icon.ico")]


def build(triple: str) -> Path:
    dist = WORK / "dist"
    command = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "bmail_entry.py"),
        "--name", "bmail", "--onedir", "--console", "--noconfirm", "--clean",
        "--distpath", str(dist), "--workpath", str(WORK / "work"), "--specpath", str(WORK),
        # The UI, the packs, the template recipes and the schema are package data.
        "--collect-data", "bmail",
        # Connectors and stages are imported lazily, by name, at run time.
        "--collect-submodules", "bmail",
        "--copy-metadata", "bmail",
        *icon_arguments(triple),
    ]
    for module in EXCLUDE:
        command += ["--exclude-module", module]
    print("building:", " ".join(command[2:5]), "…", flush=True)
    subprocess.run(command, check=True, cwd=ROOT)
    shutil.rmtree(ENGINE, ignore_errors=True)
    # Real files, no links: the installer copies this folder as it is, and not
    # every bundler keeps a symbolic link (macOS builds of Python contain them).
    # The smoke test below runs from this copy, so it proves what ships.
    shutil.copytree(dist / "bmail", ENGINE, symlinks=False)
    size = sum(f.stat().st_size for f in ENGINE.rglob("*") if f.is_file()) / 1_000_000
    target = engine_path(triple)
    print(f"built {ENGINE.relative_to(ROOT)}/ ({size:.1f} MB, {target.name} to start)", flush=True)
    return target


def smoke_test(binary: Path) -> None:
    """Start the frozen engine the way the shell does, and use it."""
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                             timeout=120, check=True).stdout.strip()
    print("version:", version, flush=True)
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "BMAIL_HOME": home}
        process = subprocess.Popen(
            [str(binary), "serve", "--json", "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            line = process.stdout.readline()
            if not line:
                raise SystemExit(f"the engine exited without a URL:\n{process.stderr.read()}")
            announced = json.loads(line)
            base = f"http://127.0.0.1:{announced['port']}"
            token = announced["token"]

            def fetch(path: str, auth: bool = True) -> bytes:
                request = urllib.request.Request(base + path)
                if auth:
                    request.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read()

            assert json.loads(fetch("/api/health", auth=False))["status"] == "ok"
            page = fetch("/").decode()
            assert "<title>Bmail</title>" in page, "the page was not bundled"
            assert b"function" in fetch("/app.js"), "the script was not bundled"
            assert fetch("/fonts/inter-latin.woff2"), "the font was not bundled"
            assert not list(ENGINE.rglob("*selectolax*")), "selectolax (LGPL Modest) was bundled"
            state = json.loads(fetch("/api/state"))
            assert "job-seeker" in state["packs"], "the packs were not bundled"
            assert state["setup"]["first_run"] is True
            accounts = json.loads(fetch("/api/accounts"))
            print("keychain:", accounts["keychain"]["backend"] or "none",
                  "(available)" if accounts["keychain"]["available"] else "(unavailable)")
            print("smoke test passed: page, script, font, packs and API all served", flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", help="Rust target triple (default: this machine)")
    parser.add_argument("--no-test", action="store_true", help="build only")
    parser.add_argument("--test-only", action="store_true", help="test an existing build")
    args = parser.parse_args()
    triple = args.target or target_triple()
    binary = engine_path(triple) if args.test_only else build(triple)
    if not args.no_test:
        started = time.monotonic()
        smoke_test(binary)
        print(f"({time.monotonic() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
