"""Record a product film of Bmail: camera moves, a real pointer, captions.

    bmail ui --no-open --port 8800          # in one shell, note the token
    python tools/record_film.py <token>     # in another

Writes a silent raw capture. `tools/compose_score.py` writes the music and the
header of that file has the ffmpeg line that marries the two.

Chromium's screencast does not capture the OS cursor — there is no cursor in
the frames at all — and the stripped ffmpeg here has no x11grab to record a
real display instead. So the pointer is drawn: the actual system arrow, at the
actual size, with the hotspot at the tip, switching to the hand over anything
clickable exactly as the real one does.

Motion is the other half of looking real. A cursor that slides at a constant
speed in a straight line reads as a machine immediately. Every move here
travels a slight arc and eases in and out, because a hand accelerates away
from where it was and slows into where it is going.

Everything is styled through the CSSOM: the page is served under
`style-src 'self'` and a style attribute would be refused.
"""
import argparse
import importlib.util
import math
import os
import pathlib
import time

from launch_cut import Cine, Frames
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent


def chromium() -> str | None:
    """A Chromium kept outside Playwright's own cache, if this machine has one.

    None means the one `python -m playwright install chromium` put in the
    cache, which Playwright finds by itself on every platform."""
    root = pathlib.Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium*/chrome-linux/chrome",
                    "chromium*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
        for found in sorted(root.glob(pattern)):
            return str(found)
    return None


ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("token", help="session token printed by `bmail ui`")
ap.add_argument("--url", default="http://127.0.0.1:8800", help="where the daemon is listening")
ap.add_argument("--out", default="film", help="directory for the raw capture")
ap.add_argument("--width", type=int, default=1920)
ap.add_argument("--height", type=int, default=1080)
ap.add_argument(
    "--cut", choices=("full", "short", "launch"), default="full",
    help="full is the 1:29 film; short is a ~30s version for a README, where "
         "the file loads on every visit and weight matters more than coverage; "
         "launch is a one-minute film for a landing page, the app as a window "
         "in a dark room, captured as frames rather than as a video.")
ap.add_argument(
    "--raster", type=int, default=3,
    help="device scale factor. The camera is a CSS transform, and Chromium "
         "rasterises a transformed layer once and then scales the texture — so "
         "this has to exceed the deepest push or every zoom is a blur of a 1x "
         "bitmap. 3 leaves 2x to spare at a 1.5x push.")
ap.add_argument(
    "--slow", type=float, default=4.0,
    help="launch cut only: act the film out this many times slower than it "
         "runs and wind it back up when the frames are joined. A browser "
         "drawing ten frames a second then gives forty, with no change to "
         "what happens in them.")
ap.add_argument(
    "--bpm", type=float, default=140.0,
    help="launch cut only: tempo of the music it is cut to. The shot list is "
         "written in bars, so every cut, click and camera move falls on this grid.")
ap.add_argument(
    "--downbeat", type=float, default=0.873,
    help="launch cut only: seconds into the music at which its first bar "
         "starts. The film starts with the music, so this much of it runs "
         "before the first word.")
ap.add_argument(
    "--gpu", action="store_true",
    help="draw with the graphics card instead of in software. Roughly twice "
         "the frame rate where there is one; leave it off on a machine without.")
args = ap.parse_args()

BASE = args.url
TOKEN = args.token
OUT = pathlib.Path(args.out)
EXE = chromium()
W, H = args.width, args.height
LAUNCH = args.cut == "launch"
# How many seconds pass on the clock for each second of film. Every wait in the
# shot list, and every move of the hand, is stretched by it.
SLOW = args.slow if LAUNCH else 1.0

STAGE = r"""([W, H, SLOW = 1]) => {
  const css = (n, p) => { for (const [k, v] of Object.entries(p)) n.style[k] = v; return n; };
  const make = (tag, p) => css(document.createElement(tag), p);
  const NS = "http://www.w3.org/2000/svg";
  const shell = document.querySelector("#shell");
  css(shell, { transformOrigin: "0 0", willChange: "transform", backfaceVisibility: "hidden" });

  // ---- the pointer ---------------------------------------------------------
  // The system arrow and the system hand, drawn to size. The hotspot of the
  // arrow is its tip at 0,0; the hand's is the fingertip, so it carries an
  // offset — get that wrong and every click lands visibly off the mark.
  const svg = (paths, w, h) => {
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", `0 0 ${w} ${h}`);
    s.setAttribute("width", w); s.setAttribute("height", h);
    for (const [d, fill, stroke] of paths) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", fill);
      if (stroke) { p.setAttribute("stroke", stroke); p.setAttribute("stroke-width", "1.25"); }
      p.setAttribute("stroke-linejoin", "round");
      s.append(p);
    }
    return s;
  };
  const ARROW = svg([[
    "M1.5 1.2 L1.5 21.4 L6.6 16.5 L10.1 24.2 L13.6 22.6 L10.2 15.1 L16.8 14.6 Z",
    "#0B0B0D", "#FFFFFF",
  ]], 22, 27);
  const HAND = svg([[
    "M9.6 2.2c1 0 1.8.8 1.8 1.8v6.3h.9V8.6c0-.9.8-1.7 1.7-1.7s1.7.8 1.7 1.7v1.7h.9V9.6c0-.9.8-1.7 1.7-1.7"
    + "s1.7.8 1.7 1.7v1.4h.9v-.7c0-.9.7-1.6 1.6-1.6s1.6.7 1.6 1.6v6.1c0 4-2.6 7.1-6.8 7.1h-2.1"
    + "c-2.5 0-4-1-5.3-2.8L5 15.4c-.5-.8-.3-1.8.5-2.3.7-.5 1.7-.3 2.2.4l.1.2V4c0-1 .8-1.8 1.8-1.8z",
    "#0B0B0D", "#FFFFFF",
  ]], 27, 27);

  const cursor = make("div", {
    position: "fixed", left: "0", top: "0", zIndex: "2147483000",
    pointerEvents: "none", willChange: "transform",
    filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,.35))",
  });
  cursor.append(ARROW);
  document.body.append(cursor);

  let mx = W / 2, my = H / 2, shape = "arrow";
  const OFFSET = { arrow: [0, 0], hand: [-9, -2] };
  const place = () => {
    const [ox, oy] = OFFSET[shape];
    cursor.style.transform = `translate(${mx + ox}px, ${my + oy}px)`;
  };
  const reshape = () => {
    const el = document.elementFromPoint(mx, my);
    const want = el && getComputedStyle(el).cursor === "pointer" ? "hand" : "arrow";
    if (want === shape) return;
    shape = want;
    cursor.replaceChildren(want === "hand" ? HAND : ARROW);
  };
  place();
  addEventListener("mousemove", (e) => { mx = e.clientX; my = e.clientY; place(); reshape(); }, true);
  addEventListener("mousedown", () => {
    // The real pointer dips a little under a click; without it a click is
    // invisible in a silent recording.
    cursor.style.transition = "transform 70ms ease-out";
    const [ox, oy] = OFFSET[shape];
    cursor.style.transform = `translate(${mx + ox}px, ${my + oy}px) scale(.88)`;
    setTimeout(() => { cursor.style.transition = ""; place(); }, 120 * SLOW);
  }, true);

  // ---- caption, along the bottom ------------------------------------------
  const cap = make("div", {
    position: "fixed", left: "50%", bottom: "52px", transform: "translate(-50%, 12px)",
    maxWidth: "70ch", padding: "13px 24px", borderRadius: "12px",
    background: "rgba(13,13,15,.94)", color: "#fff", zIndex: "2147482000",
    font: "400 20px/1.45 -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif",
    letterSpacing: "-.012em", textAlign: "center", pointerEvents: "none", opacity: "0",
    boxShadow: "0 16px 48px -14px rgba(0,0,0,.55)",
    transition: "opacity 380ms ease, transform 380ms cubic-bezier(.2,.7,.2,1)",
  });
  document.body.append(cap);

  // ---- title cards ---------------------------------------------------------
  const card = make("div", {
    position: "fixed", inset: "0", zIndex: "2147483002", pointerEvents: "none",
    display: "flex", flexDirection: "column", alignItems: "center",
    justifyContent: "center", gap: "12px", background: "#0D0D0F", opacity: "0",
    transition: "opacity 600ms ease",
  });
  const big = make("div", {
    font: "700 64px/1.1 -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif",
    letterSpacing: "-.035em", color: "#fff", transform: "translateY(12px)",
    transition: "transform 760ms cubic-bezier(.2,.7,.2,1)",
  });
  const sub = make("div", {
    font: "400 22px/1.4 -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif",
    color: "rgba(255,255,255,.6)", letterSpacing: "-.01em", textAlign: "center",
    transform: "translateY(12px)",
    transition: "transform 760ms cubic-bezier(.2,.7,.2,1) 80ms",
  });
  card.append(big, sub);
  document.body.append(card);

  // ---- camera --------------------------------------------------------------
  const cam = { s: 1, tx: 0, ty: 0 };
  // The drawer and its scrim live on <body>, outside the shell, so the camera
  // moves them too. Each is fixed to the viewport; an origin at the viewport's
  // corner, expressed in the element's own box, makes the same transform land
  // it where it would be if it were part of the shell.
  const apply = () => {
    const t = `translate(${cam.tx}px, ${cam.ty}px) scale(${cam.s})`;
    shell.style.transform = t;
    for (const n of document.querySelectorAll(".drawer, .drawer-scrim")) {
      n.style.animation = "none";
      n.style.transformOrigin = `${-n.offsetLeft}px ${-n.offsetTop}px`;
      n.style.transform = t;
    }
  };
  // Never pan past the content's edges, or a push near a corner shows a band
  // of empty page down one side.
  const solve = (s, cx, cy) => ({
    s,
    tx: Math.min(0, Math.max(W - s * W, W / 2 - s * cx)),
    ty: Math.min(0, Math.max(H - s * H, H / 2 - s * cy)),
  });
  const ease = (t) => (t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const drive = (target, ms) => {
    const from = { ...cam }, start = performance.now();
    return new Promise((done) => {
      const step = (now) => {
        const t = Math.min(1, (now - start) / ms), e = ease(t);
        cam.s = from.s + (target.s - from.s) * e;
        cam.tx = from.tx + (target.tx - from.tx) * e;
        cam.ty = from.ty + (target.ty - from.ty) * e;
        apply();
        t < 1 ? requestAnimationFrame(step) : done();
      };
      requestAnimationFrame(step);
    });
  };

  window.__film = {
    /* A shot nobody is clicking in reads better without a pointer parked in it. */
    pointer(on) { cursor.style.visibility = on ? "visible" : "hidden"; },
    say(t) { cap.textContent = t; cap.style.opacity = "1"; cap.style.transform = "translate(-50%, 0)"; },
    hush() { cap.style.opacity = "0"; cap.style.transform = "translate(-50%, 12px)"; },
    title(h, s) {
      big.textContent = h; sub.textContent = s || "";
      card.style.opacity = "1";
      requestAnimationFrame(() => { big.style.transform = "translateY(0)"; sub.style.transform = "translateY(0)"; });
    },
    drop() {
      card.style.opacity = "0";
      setTimeout(() => { big.style.transform = "translateY(12px)"; sub.style.transform = "translateY(12px)"; }, 640);
    },
    /* The rect says where a thing is on SCREEN, which is what a click needs.
       The camera needs where it sits in the LAYOUT, so undo the transform. */
    push(sel, s, ms, index = 0) {
      const node = document.querySelectorAll(sel)[index];
      if (!node) return this.pull(ms);
      const r = node.getBoundingClientRect();
      return drive(solve(s, (r.left + r.width / 2 - cam.tx) / cam.s,
                            (r.top + r.height / 2 - cam.ty) / cam.s), ms);
    },
    pull(ms) { return drive(solve(1, W / 2, H / 2), ms); },
    glide(top, ms) {
      const main = document.querySelector("#main");
      const from = main.scrollTop, start = performance.now();
      return new Promise((done) => {
        const step = (now) => {
          const t = Math.min(1, (now - start) / ms);
          main.scrollTop = from + (top - from) * ease(t);
          t < 1 ? requestAnimationFrame(step) : done();
        };
        requestAnimationFrame(step);
      });
    },
  };
}"""


class Hand:
    """A pointer that moves the way one does: along a slight arc, easing out of
    where it was and into where it is going."""

    def __init__(self, page):
        self.page = page
        self.x, self.y = W / 2, H / 2

    @staticmethod
    def _ease(t):
        return 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2

    def to(self, x, y, ms=760):
        x0, y0 = self.x, self.y
        dx, dy = x - x0, y - y0
        span = math.hypot(dx, dy)
        if span < 2:
            return
        # Control point pushed off the straight line, so the path bows.
        bow = min(90, span * 0.13)
        cx = (x0 + x) / 2 - dy / span * bow
        cy = (y0 + y) / 2 + dx / span * bow
        frames = max(14, min(52, int(ms / 22)))
        for i in range(1, frames + 1):
            t = self._ease(i / frames)
            u = 1 - t
            px = u * u * x0 + 2 * u * t * cx + t * t * x
            py = u * u * y0 + 2 * u * t * cy + t * t * y
            self.page.mouse.move(px, py)
            self.page.wait_for_timeout(ms / frames * SLOW)
        self.x, self.y = x, y

    def at(self, sel, i=0, ms=760):
        box = self.page.locator(sel).nth(i).bounding_box()
        if not box:
            return None
        self.to(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, ms)
        return box

    def click(self, sel, i=0, ms=760, settle=1200):
        if self.at(sel, i, ms) is None:
            return
        self.page.wait_for_timeout(330 * SLOW)
        self.page.mouse.down()
        self.page.wait_for_timeout(85 * SLOW)
        self.page.mouse.up()
        self.page.wait_for_timeout(settle * SLOW)


class Reel:
    """The director's controls. Thin wrappers over the stage installed above,
    so the shot list below reads as a shot list."""

    def __init__(self, page):
        self.page = page
        self.hand = Hand(page)

    def say(self, text):
        self.page.evaluate("t => window.__film.say(t)", text)

    def hush(self):
        self.page.evaluate("() => window.__film.hush()")

    def title(self, head, tail=""):
        self.page.evaluate("a => window.__film.title(a[0], a[1])", [head, tail])

    def drop(self):
        self.page.evaluate("() => window.__film.drop()")

    def push(self, selector, scale, ms, index=0):
        """Move the camera in on something."""
        self.page.evaluate(
            "a => window.__film.push(a[0], a[1], a[2], a[3])", [selector, scale, ms, index]
        )

    def pull(self, ms):
        """Back out to the whole screen."""
        self.page.evaluate("ms => window.__film.pull(ms)", ms)

    def beat(self, ms):
        self.page.wait_for_timeout(ms * SLOW)

    def roll(self, take, cine):
        """Start the picture, and the clock the shot list is written against.
        The stage's clock is the one that counts: the frames and this clock
        are both set by the moment it says it started."""
        take.start()
        take.began = cine.roll()
        self.zero = time.monotonic() - (time.time() - take.began)
        self.late = 0.0

    def at(self, second):
        """Hold until this second of the film. Waiting on the clock rather than
        for a length of time is what keeps a cut on its beat: every wait before
        it runs a little long, and this is where that is taken back."""
        wait = self.zero + second * SLOW - time.monotonic()
        if wait > 0:
            self.page.wait_for_timeout(wait * 1000)
        else:
            self.late = max(self.late, -wait / SLOW)

    def press(self, key, ms=900):
        self.page.keyboard.press(key)
        self.beat(ms)

    def typed(self, text, per_key=105):
        for character in text:
            self.page.keyboard.type(character)
            self.beat(per_key)

    def theme(self, name, ms):
        self.page.evaluate("n => { applyTheme(n); render(); }", name)
        self.beat(ms)

    def scheme(self, name, ms):
        """Light or dark, the way the operating system would switch it."""
        self.page.emulate_media(color_scheme=name)
        self.beat(ms)

    def line(self, text, hold):
        """A caption, held, then cleared."""
        self.say(text)
        self.beat(hold)
        self.hush()
        self.beat(450)

    def pointer(self, shown):
        self.page.evaluate("on => window.__film.pointer(on)", shown)


def stamp_path():
    """The mark's outline, from the script that draws it everywhere else."""
    spec = importlib.util.spec_from_file_location(
        "make_icons", HERE.parent / "packaging" / "make_icons.py")
    icons = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(icons)
    return icons.path_data(icons.FULL)


def film_full(page):
    """The long shot list. Re-cutting means editing this, nothing else."""
    reel = Reel(page)
    hand = reel.hand

    reel.title("Bmail", "Email triage that runs on your own laptop")
    reel.beat(2700)
    reel.drop()
    reel.beat(1100)

    reel.line("Right, this is everything that needs me today.", 3000)
    reel.beat(150)

    reel.push(".stack > .card", 1.5, 1900, 0)
    reel.beat(500)
    reel.say("It pulled that deadline out of the email, and it shows me the line it got it from.")
    hand.at(".stack .quote", 0, ms=900)
    reel.beat(3400)
    reel.hush()
    reel.beat(400)
    reel.pull(1700)
    reel.beat(500)

    reel.push(".schedule", 1.45, 1800)
    reel.beat(300)
    reel.say("Every date it found, on a calendar. Press a day to see what falls on it.")
    hand.click(".calendar button.day", 0, ms=900, settle=2200)
    hand.click(".due-filter button", ms=700, settle=900)
    reel.hush()
    reel.beat(300)
    reel.pull(1700)
    reel.beat(800)

    hand.click('[data-tab="cases"]', ms=900, settle=1400)
    reel.push(".column .card", 1.4, 1900, 0)
    reel.beat(500)
    reel.line("Every application I've sent, and the stage each one actually reached.", 3200)
    reel.pull(1500)
    reel.beat(300)

    # A ghosted one: the case the line below is about.
    hand.click('.column:has(h2:text-is("Ghosted")) .card', 0, ms=800, settle=1500)
    reel.push("#drawer-title", 1.4, 1800)
    reel.beat(500)
    reel.line(
        "Applied, acknowledged, then nothing for weeks. That's the bit I lose track of.",
        3600,
    )
    reel.pull(1500)
    reel.beat(300)
    reel.press("Escape", 1000)

    hand.click('[data-tab="mail"]', ms=900, settle=1400)
    hand.click('[data-list="mail"] .row', 4, ms=800, settle=1500)
    reel.push(".context .sureness", 1.5, 1900)
    reel.beat(500)
    reel.say("If I want to know why it decided that, it's all here.")
    reel.beat(3200)
    hand.at(".context select", ms=700)
    reel.beat(700)
    reel.hush()
    reel.beat(300)
    reel.line("And if it's wrong I fix it here, and it remembers.", 2900)
    reel.pull(1700)
    reel.beat(500)

    reel.say("Or just the mail that needs me.")
    hand.click('.segmented button[aria-pressed="false"]', 0, ms=800, settle=2200)
    reel.hush()
    reel.beat(600)

    reel.press("Control+k")
    reel.say("Ctrl-K, when I already know what I'm after.")
    reel.typed("nordwind")
    reel.beat(2600)
    reel.hush()
    reel.beat(400)
    reel.press("Escape")

    hand.click('[data-tab="review"]', ms=900, settle=1500)
    reel.push(".guess", 1.45, 1800)
    reel.beat(500)
    reel.line("And when it isn't sure, it asks me instead of guessing.", 3300)
    reel.pull(1700)
    reel.beat(800)

    hand.click('[data-tab="today"]', ms=900, settle=1300)
    reel.say("It follows the system into dark mode, too.")
    reel.beat(1200)
    reel.scheme("dark", 3400)
    reel.scheme("light", 1500)
    reel.hush()
    reel.beat(800)

    reel.title("Nothing leaves the laptop.", "No cloud, no API keys, no account.")
    reel.beat(3400)


def film_short(page):
    """Thirty seconds: the things that are hard to believe without seeing
    them. It reads a deadline out of the prose, it puts every date on a
    calendar, it tracks state across a thread, and the verdict can be
    inspected. Everything else is in the long cut.

    Every hold here is about a second shorter than the long cut's. A README
    video is watched once, with the page half-read; the long cut is watched on
    purpose, and can breathe."""
    reel = Reel(page)
    hand = reel.hand

    reel.title("Bmail", "Email triage that runs on your own laptop")
    reel.beat(2400)
    reel.drop()
    reel.beat(600)

    reel.line("Everything that needs me today.", 1800)

    reel.push(".stack > .card", 1.5, 1200, 0)
    reel.beat(200)
    reel.say("It pulled that deadline out of the email, and shows me the line it came from.")
    hand.at(".stack .quote", 0, ms=650)
    reel.beat(2300)
    reel.hush()
    reel.beat(250)
    reel.pull(1000)
    reel.beat(200)

    reel.push(".schedule", 1.45, 1100)
    reel.say("Every date it found, on a calendar.")
    hand.click(".calendar button.day", 0, ms=650, settle=1500)
    reel.hush()
    reel.beat(200)
    reel.pull(1000)
    reel.beat(200)

    hand.click('[data-tab="cases"]', ms=650, settle=950)
    reel.push(".column .card", 1.4, 1200, 0)
    reel.beat(200)
    reel.line("Every application, and the stage each one reached.", 2200)
    reel.pull(1000)
    reel.beat(300)

    hand.click('[data-tab="mail"]', ms=650, settle=850)
    hand.click('[data-list="mail"] .row', 4, ms=600, settle=1050)
    reel.push(".context .sureness", 1.5, 1200)
    reel.beat(200)
    reel.line("And why it decided that, in full.", 2200)
    reel.pull(1000)
    reel.beat(350)

    reel.title("Nothing leaves the laptop.", "No cloud, no API keys, no account.")
    reel.beat(2200)


def film_launch(page, take):
    """A minute for a landing page, cut to a piece of music.

    The walkthrough cuts explain the app to someone already reading about it;
    this one has to make someone who is not reading anything want to. So it
    opens on the problem in six words, lets the mark arrive, and only then
    shows the window, as an object in a room, going square on only when there
    is something to read.

    Everything is placed in bars of the music (--bpm, --downbeat), which is
    written for this: eight quiet bars, then the beat comes in, then after
    twenty-four more it drops away again. The words take the quiet bars, the
    window arrives with the beat, each of the six things it shows gets four
    bars, and the last line has the quiet to itself. A new line of type and the
    press of a click land on a beat; a camera move ends on one.
    """
    reel = Reel(page)
    hand = reel.hand
    cine = Cine(page)
    count = 60 / args.bpm
    lead = .7       # how far ahead the stage is told; more than it is ever behind

    def bar(number, beat=1):
        """The second of the film at which a beat of a bar falls."""
        return args.downbeat + ((number - 1) * 4 + (beat - 1)) * count

    def on(when):
        """The stage, to be told now what to do at `when`."""
        reel.at(when - lead)
        return cine.at(when)

    def land(when, selector, index=0):
        """Bring the pointer over something, and press it at `when`. The camera
        has to be at rest from 1.3 seconds before, or the pointer goes to
        where the thing was."""
        reel.at(when - 1.3)
        hand.at(selector, index, ms=550)
        cine.at(when).press(selector, index)

    wide = dict(s=.74, cx=W / 2, cy=H / 2, ax=W / 2, ay=H * .435, rx=0, ry=0, o=1)
    board = {**wide, "s": .98, "cx": 780, "cy": 520, "ay": H * .47, "rx": 7, "ry": 13}

    # The window waits below the frame. The room is empty.
    cine.pose(**{**wide, "ay": H * 1.5, "rx": 24, "o": 0})
    reel.pointer(False)
    reel.beat(400)
    reel.roll(take, cine)

    # Bars 1-8: the music is holding back. Words, then the mark.
    on(bar(1)).words("Ninety-four emails.")
    on(bar(3)).words("Four needed you.")
    on(bar(5) - 1.05).words_out()
    take.mark("hit", bar(5))
    on(bar(5) - .62).logo("Mail triage that runs on your own laptop.")   # pressed flat on the bar
    on(bar(9) - .5).logo_out()

    # Bar 9: the beat comes in, and the window with it.
    take.mark("open", bar(9))
    on(bar(9)).pose(**{**wide, "ay": H * 1.05, "s": .6, "rx": 22, "ry": -16, "o": 0})
    cine.at(bar(9)).pose(ms=2400, ease="out", **{**wide, "rx": 6, "ry": -11})
    cine.at(bar(9)).pulse(count * 2000)
    on(bar(10)).line("Everything that needs you today.")
    cine.at(bar(10)).pose(ms=4000, ease="drift", **{**wide, "rx": 3, "ry": -5})
    on(bar(12)).line_out()
    on(bar(13) - 1.5).focus(".stack > .card", 1.5, 1500)

    # Bar 13: square on, close. There is something to read.
    on(bar(13)).spot(".stack .quote")
    cine.at(bar(13)).line("It reads the deadline out of the email, and shows you the line.")
    cine.at(bar(13)).pointer(True)
    reel.at(bar(13))
    hand.at(".stack .quote", 0, ms=850)
    on(bar(16)).unspot()
    cine.at(bar(16)).line_out()
    on(bar(17) - 1.5).focus(".schedule", 1.42, 1500)

    # Bar 17: the calendar.
    on(bar(17)).line("Every date it finds, on a calendar.")
    land(bar(18), ".calendar button.day")
    land(bar(19), ".due-filter button")
    on(bar(20) - 1.0).line_out()
    cine.at(bar(20) - 1.0).pose(ms=950, **wide)
    land(bar(20, 4), '[data-tab="cases"]')

    # Bar 21: across to the board, and along it.
    on(bar(21)).pose(ms=1300, ease="out", **board)
    cine.at(bar(21)).pointer(False)
    on(bar(21, 3)).line("Every application, and the stage it really reached.")
    on(bar(22)).pose(ms=1500, ease="drift", **{**board, "cx": 1000, "ry": 9})
    on(bar(24) - 1.9).line_out()
    cine.at(bar(24) - 1.9).pose(ms=950, **wide)
    cine.at(bar(24) - .9).pointer(True)
    # The screen cross-fades as it changes and takes no clicks while it does,
    # so the tab goes three beats before the message is opened.
    land(bar(24, 2), '[data-tab="mail"]')

    # Bar 25: one message, and why it was sorted the way it was.
    reel.at(bar(24, 2) + .3)
    hand.at('[data-list="mail"] .row', 4, ms=500)
    cine.at(bar(25)).press('[data-list="mail"] .row', 4)
    cine.at(bar(25) + .15).focus(".context .sureness", 1.6, 1500)
    on(bar(26)).spot(".context .sureness", 0, 12)
    cine.at(bar(26)).line("And it tells you why it decided that.")
    on(bar(28) - .5).unspot()
    cine.at(bar(28) - .5).line_out()
    cine.at(bar(28) - .5).pose(ms=850, **wide)

    # Bar 29: back to today; bar 31, the lights go down.
    land(bar(29), '[data-tab="today"]')
    cine.at(bar(29)).pointer(False)
    cine.at(bar(29) + .05).pose(ms=1500, **{**wide, "rx": 5, "ry": 10})
    on(bar(30)).line("In light, or in dark.")
    on(bar(31)).scheme("dark")
    on(bar(32, 3)).line_out()

    # Bar 33: the beat drops away. The window goes back into the room and the
    # last word has the quiet to itself.
    take.mark("close", bar(33) - .85)
    take.mark("hit", bar(33))
    on(bar(33) - .85).pose(ms=850, **{**wide, "s": .56, "ay": H * .5, "rx": 16, "ry": 10, "o": 0})
    on(bar(33)).pulse(0)
    cine.at(bar(33)).words("Nothing leaves the laptop.", "No cloud. No API keys. No account.")
    on(bar(36) - 1.1).words_out()
    on(bar(36) - .62).logo("Free and open source.", "github.com/GajjarB/Bmail")
    on(bar(38, 3)).fade(850)
    reel.at(bar(39))
    told = cine.report()
    print(f"recorder behind its clock by at most {reel.late * 1000:.0f} ms of film; "
          f"stage told too late {told['missed']} times (worst {told['worst']} ms)")


# The headless shell Playwright launches by default only draws in software. The
# full browser, still headless, will use the card if asked. Not a real window:
# one larger than the screen it is on stops taking clicks beyond the screen's edge.
GPU = ["--enable-gpu", "--ignore-gpu-blocklist"]
if os.name == "nt":
    GPU.append("--use-angle=d3d11")

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        executable_path=EXE, channel="chromium" if args.gpu and not EXE else None,
        args=["--no-sandbox", "--hide-scrollbars", *(GPU if args.gpu else [])])
    ctx = browser.new_context(
        viewport={"width": W, "height": H},
        device_scale_factor=args.raster,
        # The launch cut keeps its own frames; see Frames.
        **({} if LAUNCH else {"record_video_dir": str(OUT / "raw"),
                              "record_video_size": {"width": W, "height": H}}),
    )
    page = ctx.new_page()
    page.goto(f"{BASE}/?token={TOKEN}", wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    page.evaluate("localStorage.setItem('bmail.window','3650'); localStorage.setItem('bmail.theme','studio')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    page.wait_for_timeout(1500)
    page.evaluate(STAGE, [W, H, SLOW])
    page.wait_for_timeout(700)
    if LAUNCH:
        page.evaluate((HERE / "launch_stage.js").read_text(encoding="utf-8"),
                      [W, H, stamp_path()])
        page.wait_for_timeout(400)
        take = Frames(ctx, page, OUT, (W, H), SLOW)
        film_launch(page, take)
        path = take.finish()
    else:
        (film_short if args.cut == "short" else film_full)(page)
        path = page.video.path()
    ctx.close()
    browser.close()
print(path)
