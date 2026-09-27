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

import pytest

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


def test_every_file_agrees_on_the_version():
    """Seven files in three ecosystems carry the version. An installer built
    from files that disagree reports one version and updates to another."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "set_version", pathlib.Path(__file__).resolve().parents[1] / "packaging" / "set_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = module.read_all()
    assert len(set(found.values())) == 1, found
    import bmail

    assert set(found.values()) == {bmail.__version__}


def test_the_version_being_released_has_release_notes():
    """The release build stops without them; find out here instead."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_notes", ROOT / "packaging/release_notes.py")
    notes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notes)
    text = notes.notes(notes.current_version())
    assert "### Install" in text
    assert "## " not in text.replace("### ", ""), "the next version's notes leaked in"
    # Printed on Windows runners, whose console is not UTF-8.
    text.encode("ascii")


def test_release_notes_are_the_versions_own_section():
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_notes", ROOT / "packaging/release_notes.py")
    notes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notes)
    changelog = "# Changelog\n\n## 1.1.0\n\nNewer.\n\n## 1.0.0\n\nOlder.\n"
    assert notes.section("1.0.0", changelog) == "Older."
    assert notes.section("1.1.0", changelog) == "Newer."
    with pytest.raises(SystemExit):
        notes.section("2.0.0", changelog)
