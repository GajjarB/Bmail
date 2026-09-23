"""One version number, written everywhere it lives.

The version appears in seven files across three ecosystems — the Python
package, the Rust shell, Tauri's config and npm's manifests — and an installer
built from files that disagree reports one version and updates to another.

    python packaging/set_version.py 0.2.0        # set it everywhere
    python packaging/set_version.py --check      # all files agree?
    python packaging/set_version.py --check v0.2.0   # ...and match this tag?
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

# (file, pattern whose group 1 is the version) for the plain-text files.
TEXT = [
    ("pyproject.toml", r'(?m)^version = "([^"]+)"'),
    ("src/bmail/__init__.py", r'(?m)^__version__ = "([^"]+)"'),
    ("desktop/src-tauri/Cargo.toml", r'(?m)^version = "([^"]+)"'),
    ("desktop/src-tauri/Cargo.lock", r'name = "bmail-desktop"\nversion = "([^"]+)"'),
]
JSON = [
    ("desktop/src-tauri/tauri.conf.json", [("version",)]),
    ("desktop/package.json", [("version",)]),
    ("desktop/package-lock.json", [("version",), ("packages", "", "version")]),
]


def read_all() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, pattern in TEXT:
        match = re.search(pattern, (ROOT / name).read_text(encoding="utf-8"))
        found[name] = match.group(1) if match else "(missing)"
    for name, paths in JSON:
        data = json.loads((ROOT / name).read_text(encoding="utf-8"))
        for path in paths:
            node = data
            for key in path:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            found[name + ("" if len(path) == 1 else ":" + "/".join(path))] = (
                node if isinstance(node, str) else "(missing)")
    return found


def write_all(version: str) -> None:
    for name, pattern in TEXT:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        match = re.search(pattern, text)
        if not match:
            raise SystemExit(f"{name}: no version found")
        start, end = match.span(1)
        path.write_text(text[:start] + version + text[end:], encoding="utf-8")
    for name, paths in JSON:
        path = ROOT / name
        data = json.loads(path.read_text(encoding="utf-8"))
        for keys in paths:
            node = data
            for key in keys[:-1]:
                node = node[key]
            node[keys[-1]] = version
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--check":
        found = read_all()
        versions = set(found.values())
        expected = argv[1].removeprefix("v") if len(argv) > 1 else None
        if len(versions) == 1 and (expected is None or versions == {expected}):
            print(f"version {versions.pop()} everywhere")
            return 0
        for name, version in found.items():
            print(f"  {version:12} {name}")
        print(f"versions disagree{f' with the tag {argv[1]}' if expected else ''}", file=sys.stderr)
        return 1
    if len(argv) != 1 or not SEMVER.match(argv[0].removeprefix("v")):
        print(__doc__, file=sys.stderr)
        return 2
    version = argv[0].removeprefix("v")
    write_all(version)
    print(f"set {version} in {len(read_all())} places; commit, then tag v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
