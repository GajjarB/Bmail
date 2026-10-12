"""The release notes for one version: its section of CHANGELOG.md, and how to
install it.

The release build puts this on the release page, and a build that carries the
updater shows it to installed copies under "What's new". Writing the notes
once, in the changelog, keeps the two from saying different things.

    python packaging/release_notes.py 0.2.0
    python packaging/release_notes.py          # the version in tauri.conf.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INSTALL = """\
### Install

- **Windows**: the `-setup.exe` (no administrator rights needed), or the `.msi`
- **macOS** (Apple silicon): the `.dmg`
- **Linux**: the `.AppImage` (any distribution), or the `.deb` (Debian, Ubuntu)

Already have Bmail? Run the new installer over it: your mail and settings are
kept. Everything runs on your computer. Your mail is never uploaded.

If this build is not signed, your system warns you the first time you open it.
On Windows: More info, then Run anyway. On macOS: System Settings, Privacy &
Security, Open Anyway.
"""


def section(version: str, changelog: str) -> str:
    """The text under `## <version>`, up to the next version's heading."""
    match = re.search(rf"(?m)^## {re.escape(version)}\s*\n(.*?)(?=^## |\Z)", changelog, re.S)
    if not match:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return match.group(1).strip()


def notes(version: str) -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return f"{section(version, changelog)}\n\n{INSTALL}"


def current_version() -> str:
    conf = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    return str(conf["version"])


if __name__ == "__main__":
    print(notes(sys.argv[1].lstrip("v") if len(sys.argv) > 1 else current_version()))
