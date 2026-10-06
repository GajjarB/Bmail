"""One mark, drawn once and written everywhere it lives.

The mark is a postage stamp carrying a B: cream paper, tilted, on an ink tile.
It appears as the README logo, the browser tab's favicon, the mark in the app's
rail and start screen, the desktop shell's loading page and six icon files the
installers are built from. Copies redrawn by hand drift, so all of them are
derived from the geometry below.

    python packaging/make_icons.py            # write the logo and the desktop icons
    python packaging/make_icons.py --check    # do the inline copies match?
    python packaging/make_icons.py --inline   # print the path the inline copies carry

Writing the icon files needs Pillow (`pip install pillow`); nothing else does.
"""

from __future__ import annotations

import io
import math
import struct
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "desktop" / "src-tauri" / "icons"

GRID = 64                 # every length below is in these units
TILE = "#10111A"
TILE_EDGE = "#2C2E3A"     # a hairline, so the tile keeps its edge on a dark taskbar
PAPER = "#F4ECDF"
TILE_RADIUS = 15
TILT = -6                 # degrees: a stamp stuck on by hand is never square

Point = tuple[float, float]
# A closed outline: where it starts, then relative steps. A step is a line
# ("l", dx, dy) or a circular arc under half a turn ("a", radius, sweep, dx, dy).
Outline = tuple[Point, list[tuple]]


class Variant(NamedTuple):
    """How the mark is drawn at one range of sizes."""

    stamp: Point          # width, height
    notch: float          # radius of one perforation
    pitch: float          # distance between perforations, roughly
    corner: float
    letter: float         # scale of the B


# Perforations a pixel wide turn to fuzz, so small sizes get fewer, larger ones
# and a bigger letter.
FULL = Variant(stamp=(40, 50), notch=2.0, pitch=6.5, corner=1.2, letter=0.72)
SMALL = Variant(stamp=(42, 52), notch=2.6, pitch=8.4, corner=1.5, letter=0.80)
SMALL_BELOW = 64          # pixel size under which SMALL is drawn

# Files that carry the small mark inline, because each has to paint before
# anything else can be fetched.
INLINE = [
    "src/bmail/ui/static/index.html",
    "src/bmail/ui/static/app.js",
    "desktop/dist/index.html",
]


# ---- geometry ---------------------------------------------------------------- #

def stamp_outline(variant: Variant) -> Outline:
    """A rectangle about the origin with perforations bitten out of each edge."""
    width, height = variant.stamp
    corner, notch = variant.corner, variant.notch
    steps: list[tuple] = []
    turns = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    for index, (dx, dy) in enumerate(turns):
        length = (width if dx else height) - 2 * corner
        count = max(1, round(length / variant.pitch))
        flat = (length / count - 2 * notch) / 2
        for _ in range(count):
            steps.append(("l", dx * flat, dy * flat))
            steps.append(("a", notch, 0, dx * 2 * notch, dy * 2 * notch))
            steps.append(("l", dx * flat, dy * flat))
        nx, ny = turns[(index + 1) % 4]
        steps.append(("a", corner, 1, (dx + nx) * corner, (dy + ny) * corner))
    return (-width / 2 + corner, -height / 2), _merged(steps)


def letter_outlines(scale: float) -> list[Outline]:
    """A bold B about the origin: the letter, then its two counters."""
    # Drawn on a 27.5 x 40 body: a stem, a bowl of radius 10 and a wider one of
    # radius 11 below it. Where the two bowls cross is the waist.
    top, bottom = ((13.5, 10.0), 10.0), ((16.5, 29.0), 11.0)
    waist = _crossing(top, bottom)
    outer: Outline = ((3.0, 0.0), [
        ("l", 10.5, 0),
        ("a", 10.0, 1, waist[0] - 13.5, waist[1]),
        ("a", 11.0, 1, 16.5 - waist[0], 40.0 - waist[1]),
        ("l", -13.5, 0),
        ("a", 3.0, 1, -3.0, -3.0),
        ("l", 0, -34.0),
        ("a", 3.0, 1, 3.0, -3.0),
    ])
    upper: Outline = ((8.0, 7.5), [
        ("l", 4.5, 0), ("a", 3.75, 1, 0, 7.5), ("l", -4.5, 0), ("l", 0, -7.5)])
    lower: Outline = ((8.0, 22.5), [
        ("l", 7.25, 0), ("a", 5.0, 1, 0, 10.0), ("l", -7.25, 0), ("l", 0, -10.0)])
    return [_placed(outline, scale, (-13.75, -20.0)) for outline in (outer, upper, lower)]


def mark_outlines(variant: Variant) -> list[Outline]:
    """Stamp, letter and counters. Filled even-odd, the letter is a hole in the paper."""
    return [stamp_outline(variant), *letter_outlines(variant.letter)]


def _merged(steps: list[tuple]) -> list[tuple]:
    """Join consecutive lines that run the same way."""
    out: list[tuple] = []
    for step in steps:
        if out and step[0] == "l" and out[-1][0] == "l":
            _, ax, ay = out[-1]
            _, bx, by = step
            if abs(ax * by - ay * bx) < 1e-9:
                out[-1] = ("l", ax + bx, ay + by)
                continue
        out.append(step)
    return out


def _crossing(first: tuple[Point, float], second: tuple[Point, float]) -> Point:
    """The right-hand point where two circles meet."""
    (ax, ay), ar = first
    (bx, by), br = second
    gap = math.hypot(bx - ax, by - ay)
    along = (ar * ar - br * br + gap * gap) / (2 * gap)
    across = math.sqrt(ar * ar - along * along)
    ux, uy = (bx - ax) / gap, (by - ay) / gap
    return ax + ux * along + uy * across, ay + uy * along - ux * across


def _placed(outline: Outline, scale: float, shift: Point) -> Outline:
    (x, y), steps = outline
    scaled = [
        (step[0], step[1] * scale, step[2] * scale) if step[0] == "l"
        else ("a", step[1] * scale, step[2], step[3] * scale, step[4] * scale)
        for step in steps
    ]
    return ((x + shift[0]) * scale, (y + shift[1]) * scale), scaled


def flatten(outline: Outline, detail: float = 0.05) -> list[Point]:
    """The outline as a polygon, arcs cut into chords no longer than `detail`."""
    (x, y), steps = outline
    points = [(x, y)]
    for step in steps:
        if step[0] == "l":
            x, y = x + step[1], y + step[2]
            points.append((x, y))
            continue
        _, radius, sweep, dx, dy = step
        chord = math.hypot(dx, dy)
        rise = math.sqrt(max(radius * radius - chord * chord / 4, 0.0))
        # With y pointing down, a clockwise arc keeps its centre on the right.
        side = 1 if sweep else -1
        cx = x + dx / 2 - side * dy / chord * rise
        cy = y + dy / 2 + side * dx / chord * rise
        start = math.atan2(y - cy, x - cx)
        turn = (math.atan2(y + dy - cy, x + dx - cx) - start) % (2 * math.pi)
        if not sweep:
            turn -= 2 * math.pi
        pieces = max(2, math.ceil(abs(turn) * radius / detail))
        for n in range(1, pieces + 1):
            angle = start + turn * n / pieces
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        x, y = x + dx, y + dy
    return points


# ---- SVG --------------------------------------------------------------------- #

def _num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def path_data(variant: Variant) -> str:
    """The whole mark as one SVG path, laid out about the middle of the grid."""
    parts: list[str] = []
    for (x, y), steps in mark_outlines(variant):
        parts.append(f"M{_num(x + GRID / 2)} {_num(y + GRID / 2)}")
        for step in steps:
            if step[0] == "a":
                _, radius, sweep, dx, dy = step
                parts.append(f"a{_num(radius)} {_num(radius)} 0 0 {sweep} {_num(dx)} {_num(dy)}")
            elif abs(step[2]) < 1e-9:
                parts.append(f"h{_num(step[1])}")
            elif abs(step[1]) < 1e-9:
                parts.append(f"v{_num(step[2])}")
            else:
                parts.append(f"l{_num(step[1])} {_num(step[2])}")
        parts.append("z")
    return "".join(parts)


def logo_svg() -> str:
    half = GRID // 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}" '
        'width="72" height="72">\n'
        f'  <rect x=".5" y=".5" width="{GRID - 1}" height="{GRID - 1}" rx="{TILE_RADIUS - 0.5}" '
        f'fill="{TILE}" stroke="{TILE_EDGE}"/>\n'
        f'  <path transform="rotate({TILT} {half} {half})" fill="{PAPER}" fill-rule="evenodd" '
        f'd="{path_data(FULL)}"/>\n'
        "</svg>\n"
    )


# ---- pixels ------------------------------------------------------------------ #

def render(size: int):
    """The mark as a `size` x `size` image, drawn large and scaled down."""
    from PIL import Image, ImageDraw

    variant = SMALL if size < SMALL_BELOW else FULL
    oversample = max(4, math.ceil(512 / size))
    unit = size * oversample / GRID
    canvas = Image.new("RGBA", (size * oversample,) * 2, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    edge = size * oversample - 1
    draw.rounded_rectangle((0, 0, edge, edge), radius=TILE_RADIUS * unit, fill=TILE_EDGE)
    draw.rounded_rectangle((unit, unit, edge - unit, edge - unit),
                           radius=(TILE_RADIUS - 1) * unit, fill=TILE)

    cos, sin = math.cos(math.radians(TILT)), math.sin(math.radians(TILT))
    middle = GRID / 2
    for index, outline in enumerate(mark_outlines(variant)):
        polygon = [((x * cos - y * sin + middle) * unit, (x * sin + y * cos + middle) * unit)
                   for x, y in flatten(outline)]
        draw.polygon(polygon, fill=TILE if index == 1 else PAPER)   # 1 is the letter
    return canvas.resize((size, size), Image.LANCZOS)


def write_icons() -> list[Path]:
    written = []
    for name, size in [("32x32.png", 32), ("128x128.png", 128),
                       ("128x128@2x.png", 256), ("icon.png", 512)]:
        render(size).save(ICONS / name)
        written.append(ICONS / name)
    # Pillow scales one image to every size it writes, which would shrink the
    # detailed drawing into the small slots; give it each size drawn for itself.
    ico = [render(size) for size in (16, 24, 32, 48, 64, 256)]
    ico[-1].save(ICONS / "icon.ico", sizes=[image.size for image in ico], append_images=ico[:-1])
    (ICONS / "icon.icns").write_bytes(icns_bytes())
    return [*written, ICONS / "icon.ico", ICONS / "icon.icns"]


def icns_bytes() -> bytes:
    """An .icns holding every size macOS asks for.

    Written by hand because Pillow leaves out the 16 and 32 pixel entries, and
    macOS then shrinks a larger drawing into the menu bar and list views.
    """
    def png(size: int) -> bytes:
        buffer = io.BytesIO()
        render(size).save(buffer, "PNG")
        return buffer.getvalue()

    chunks = [(b"ic07", png(128)), (b"ic08", png(256)), (b"ic09", png(512)), (b"ic10", png(1024)),
              (b"ic11", png(32)), (b"ic12", png(64)), (b"ic13", png(256)), (b"ic14", png(512))]
    # The two smallest sizes predate PNG entries: packed colour, then a plain alpha mask.
    for size, colour, mask in ((16, b"is32", b"s8mk"), (32, b"il32", b"l8mk")):
        *channels, alpha = render(size).split()
        chunks.append((colour, b"".join(_packed(channel.tobytes()) for channel in channels)))
        chunks.append((mask, alpha.tobytes()))
    body = b"".join(kind + struct.pack(">I", len(data) + 8) + data for kind, data in chunks)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def _packed(data: bytes) -> bytes:
    """The .icns run-length scheme: a count byte, then literals or one repeated byte."""
    out = bytearray()
    at = 0
    while at < len(data):
        run = 1
        while at + run < len(data) and run < 130 and data[at + run] == data[at]:
            run += 1
        if run >= 3:
            out += bytes((run + 125, data[at]))
            at += run
            continue
        end = at + 1
        while end < len(data) and end - at < 128 and not (
                end + 2 < len(data) and data[end] == data[end + 1] == data[end + 2]):
            end += 1
        out += bytes((end - at - 1,)) + data[at:end]
        at = end
    return bytes(out)


# ---- the copies -------------------------------------------------------------- #

def stale() -> list[str]:
    """Files whose copy of the mark is not the one drawn here."""
    wrong = []
    logo = ROOT / "media" / "logo.svg"
    if not logo.exists() or logo.read_text(encoding="utf-8") != logo_svg():
        wrong.append("media/logo.svg")
    small = path_data(SMALL)
    for name in INLINE:
        if small not in (ROOT / name).read_text(encoding="utf-8"):
            wrong.append(name)
    return wrong


def main(argv: list[str]) -> int:
    if argv == ["--inline"]:
        print(path_data(SMALL))
        return 0
    if argv == ["--check"]:
        wrong = stale()
        for name in wrong:
            print(f"  out of date: {name}", file=sys.stderr)
        if not wrong:
            print("the mark is the same everywhere")
        return 1 if wrong else 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    (ROOT / "media" / "logo.svg").write_text(logo_svg(), encoding="utf-8")
    try:
        written = write_icons()
    except ImportError:
        print("media/logo.svg written; the icon files need Pillow: pip install pillow",
              file=sys.stderr)
        return 1
    print(f"wrote media/logo.svg and {len(written)} icon files")
    for name in stale():
        print(f"  still carries the old mark: {name} (paste the path from --inline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
