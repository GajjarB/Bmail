"""Everything the UI needs has to be in the wheel, not just in the checkout.

setuptools' package-data globs do not recurse: `ui/static/*` matches the files
in that folder and nothing beneath it. When the fonts moved into
`ui/static/fonts/`, an installed Bmail would have served the page and 404'd
every font, and the page would have quietly fallen back to system type —
nothing in a checkout would have shown it.
"""

from __future__ import annotations

import fnmatch
import pathlib
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "bmail"


def _globs() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return config["tool"]["setuptools"]["package-data"]["bmail"]


def test_every_ui_asset_is_declared_as_package_data():
    globs = _globs()
    static = PACKAGE / "ui" / "static"
    missing = []
    for path in static.rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(PACKAGE).as_posix()
        # fnmatch's * crosses "/", setuptools' does not; compare segment counts
        # so a glob only claims files at its own depth, as setuptools would.
        depth = relative.count("/")
        if not any(fnmatch.fnmatch(relative, g) and g.count("/") == depth for g in globs):
            missing.append(relative)
    assert not missing, f"not shipped in the wheel: {missing}"


def test_bundled_third_party_assets_carry_their_licences():
    static = PACKAGE / "ui" / "static"
    assert (static / "fonts" / "LICENSE-Inter.txt").read_text().count("SIL OPEN FONT LICENSE") >= 1
    assert "ISC License" in (static / "icons.LICENSE.txt").read_text()
