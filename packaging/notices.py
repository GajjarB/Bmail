"""Write THIRD_PARTY_NOTICES.md: every component the installers ship, its
licence, where its source is, and the licence texts themselves.

Bmail is MIT, and so is almost everything it is built from; what those
licences ask in return is that their notices travel with the program. This
collects them from the real dependency data rather than from memory:

- the Python packages frozen into the engine: the closure of Bmail's own
  requirements, read from their installed metadata, plus the packages only one
  platform installs;
- the Rust crates compiled into the desktop shell, read from `cargo metadata`
  (every platform's, so one file serves all three installers), with the licence
  files from each crate's source;
- the font, the icons, Python itself and the parts of the runtime that come
  with an installer.

A dependency under a licence not on the allowed list stops the script: nothing
copyleft gets into an installer without someone deciding it should.

    python packaging/notices.py            # write THIRD_PARTY_NOTICES.md
    python packaging/notices.py --out desktop/src-tauri/engine/THIRD_PARTY_NOTICES.md
    python packaging/notices.py --check    # fail if it is out of date

The release build writes it into the engine folder, which the installer copies
as it is, so every installed copy carries the notices for what it contains.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "THIRD_PARTY_NOTICES.md"
SHELL = ROOT / "desktop" / "src-tauri"

# Licences a shipped component may carry. Weak copyleft (MPL-2.0 per file;
# LGPL for a separate, replaceable library) is allowed with its notice; strong
# copyleft (GPL, AGPL) is not.
ALLOWED = {
    "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Zlib",
    "Unlicense", "0BSD", "CC0-1.0", "PSF-2.0", "Unicode-3.0", "Unicode-DFS-2016",
    "BSL-1.0", "CDLA-Permissive-2.0", "OFL-1.1", "MPL-2.0",
    "Apache-2.0 WITH LLVM-exception", "LGPL-2.1-or-later",
}

# The Python packages frozen into the engine (packaging/build_sidecar.py).
# selectolax is left out of the app; see EXCLUDE there.
ENGINE_EXCLUDED = {"selectolax"}
# Installed only on a platform other than the one this runs on.
PLATFORM_ONLY = [
    ("pywin32-ctypes", "BSD-3-Clause", "https://pypi.org/project/pywin32-ctypes/", "Windows"),
    ("SecretStorage", "BSD-3-Clause", "https://pypi.org/project/SecretStorage/", "Linux"),
    ("jeepney", "MIT", "https://pypi.org/project/jeepney/", "Linux"),
]

# Shipped, but not a package either ecosystem describes.
OTHER = [
    ("Python", "PSF-2.0", "https://www.python.org/downloads/source/",
     "The interpreter frozen into the engine."),
    ("PyInstaller bootloader", "GPL-2.0-or-later WITH Bootloader-exception",
     "https://github.com/pyinstaller/pyinstaller",
     "Starts the frozen engine. Its licence exception lets programs built with it "
     "be distributed under any licence."),
    ("Inter typeface", "OFL-1.1", "https://github.com/rsms/inter",
     "The interface font. Licence: src/bmail/ui/static/fonts/LICENSE-Inter.txt."),
    ("Lucide icons", "ISC", "https://github.com/lucide-icons/lucide",
     "The interface icons. Licence: src/bmail/ui/static/icons.LICENSE.txt."),
    ("GTK and WebKitGTK (Linux AppImage only)", "LGPL-2.1-or-later",
     "https://webkitgtk.org/ and https://www.gtk.org/",
     "Bundled as separate shared libraries, unmodified; any compatible version can "
     "replace them. Their source is available from the addresses given."),
    ("Microsoft Edge WebView2 (Windows)", "Microsoft Software License",
     "https://developer.microsoft.com/microsoft-edge/webview2/",
     "Part of Windows; the installer may run Microsoft's bootstrapper to install it."),
]


def spdx(expression: str) -> str:
    """Normalise the old slash style ("MIT/Apache-2.0") to SPDX."""
    return re.sub(r"\s*/\s*", " OR ", expression.strip())


def acceptable(expression: str) -> bool:
    """True when some choice the expression offers is entirely allowed."""
    expression = spdx(expression).replace("(", "").replace(")", "")
    for option in re.split(r"\s+OR\s+", expression):
        terms = [t.strip() for t in re.split(r"\s+AND\s+", option)]
        if all(term in ALLOWED for term in terms):
            return True
    return False


# --------------------------------------------------------------------------- #


def _python_packages() -> list[dict[str, str]]:
    from packaging.requirements import Requirement

    def key(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    found: dict[str, md.Distribution] = {}
    stack = [Requirement(r).name for r in md.distribution("bmail").requires or []
             if Requirement(r).marker is None
             or Requirement(r).marker.evaluate({"extra": ""})]
    while stack:
        name = stack.pop()
        if key(name) in found or key(name) in ENGINE_EXCLUDED:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        found[key(name)] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                stack.append(req.name)

    packages = []
    for _key, dist in sorted(found.items()):
        meta = dist.metadata
        expression = meta.get("License-Expression") or ""
        if not expression or len(expression) > 60:
            classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                           if c.startswith("License ::")]
            expression = _from_classifier(classifiers) or expression[:60]
        texts = [p.read_text(encoding="utf-8", errors="replace")
                 for p in _licence_files(dist)]
        packages.append({"name": meta["Name"], "version": dist.version,
                         "licence": spdx(expression),
                         "source": f"https://pypi.org/project/{meta['Name']}/{dist.version}/",
                         "texts": texts})
    for name, licence, source, where in PLATFORM_ONLY:
        if key(name) not in found:
            packages.append({"name": name, "version": f"({where} only)", "licence": licence,
                             "source": source, "texts": []})
    return packages


def _from_classifier(classifiers: list[str]) -> str:
    table = {"MIT License": "MIT", "BSD License": "BSD-3-Clause",
             "Apache Software License": "Apache-2.0",
             "Python Software Foundation License": "PSF-2.0",
             "ISC License (ISCL)": "ISC"}
    names = [table[c] for c in classifiers if c in table]
    return " OR ".join(dict.fromkeys(names))


def _licence_files(dist: md.Distribution) -> list[Path]:
    files = []
    for entry in dist.files or []:
        text = str(entry)
        if re.search(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$", text, re.I) and ".dist-info" in text:
            path = Path(dist.locate_file(entry))
            if path.is_file():
                files.append(path)
    return files


def _rust_crates() -> list[dict[str, str]]:
    raw = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--features", "updater"],
        cwd=SHELL, capture_output=True, text=True, check=True).stdout
    meta = json.loads(raw)
    used = {node["id"] for node in meta["resolve"]["nodes"]}
    crates = []
    for package in meta["packages"]:
        if package["id"] not in used or package["name"] == "bmail-desktop":
            continue
        folder = Path(package["manifest_path"]).parent
        texts = [p.read_text(encoding="utf-8", errors="replace")
                 for p in sorted(folder.iterdir())
                 if p.is_file() and re.match(r"(LICEN[CS]E|COPYING|NOTICE)", p.name, re.I)]
        crates.append({"name": package["name"], "version": package["version"],
                       "licence": spdx(package.get("license") or "see licence file"),
                       "source": f"https://crates.io/crates/{package['name']}/{package['version']}",
                       "texts": texts})
    return sorted(crates, key=lambda c: (c["name"], c["version"]))


# --------------------------------------------------------------------------- #


def render(python: list[dict], rust: list[dict]) -> str:
    problems = [f"{c['name']} {c['version']}: {c['licence']}" for c in python + rust
                if not acceptable(c["licence"])]
    if problems:
        raise SystemExit("licences not on the allowed list:\n  " + "\n  ".join(problems))

    lines = [
        "# Third-party notices",
        "",
        "Bmail is free software under the MIT licence (see LICENSE). The installers",
        "also contain the components below, each under its own licence. Every one of",
        "them is free and open source; the list says where to get each one's source.",
        "",
        "This file is generated by `packaging/notices.py`; do not edit it by hand.",
        "",
        "## Other components",
        "",
        "| Component | Licence | Source | Note |",
        "|---|---|---|---|",
    ]
    lines += [f"| {n} | {lic} | {src} | {note} |" for n, lic, src, note in OTHER]

    texts: dict[str, tuple[str, list[str]]] = {}

    def table(title: str, items: list[dict]) -> None:
        lines.extend(["", f"## {title}", "", "| Name | Version | Licence | Source |", "|---|---|---|---|"])
        for item in items:
            lines.append(f"| {item['name']} | {item['version']} | {item['licence']} | {item['source']} |")
            for text in item["texts"]:
                digest = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:12]
                entry = texts.setdefault(digest, (text.strip(), []))
                entry[1].append(f"{item['name']} {item['version']}")

    table("Python packages in the engine", python)
    table("Rust crates in the desktop shell", rust)

    lines += ["", "## Licence texts", "",
              "Each text below is reproduced as its authors wrote it, once for every "
              "component that ships it word for word."]
    for text, owners in sorted(texts.values(), key=lambda entry: entry[1][0].lower()):
        lines += ["", f"### {', '.join(owners[:6])}{' and others' if len(owners) > 6 else ''}",
                  "", "```text", text.replace("```", "'''"), "```"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    parser.add_argument("--out", type=Path, default=OUT, help="where to write it")
    args = parser.parse_args()
    text = render(_python_packages(), _rust_crates())
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("THIRD_PARTY_NOTICES.md is out of date: run python packaging/notices.py")
            return 1
        print("notices up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}: {len(text) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
