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
import math
import os
import pathlib

from playwright.sync_api import sync_playwright


def chromium() -> str:
    """Playwright's own Chromium, wherever this machine keeps it."""
    root = pathlib.Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium*/chrome-linux/chrome",
                    "chromium*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
        for found in sorted(root.glob(pattern)):
            return str(found)
    raise SystemExit(
        "No Chromium found. Install one with:  python -m playwright install chromium"
    )


ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("token", help="session token printed by `bmail ui`")
ap.add_argument("--url", default="http://127.0.0.1:8800", help="where the daemon is listening")
ap.add_argument("--out", default="film", help="directory for the raw capture")
ap.add_argument("--width", type=int, default=1920)
ap.add_argument("--height", type=int, default=1080)
ap.add_argument(
    "--cut", choices=("full", "short"), default="full",
    help="full is the 1:29 film; short is a ~30s version for a README, where "
         "the file loads on every visit and weight matters more than coverage.")
ap.add_argument(
    "--raster", type=int, default=3,
    help="device scale factor. The camera is a CSS transform, and Chromium "
         "rasterises a transformed layer once and then scales the texture — so "
         "this has to exceed the deepest push or every zoom is a blur of a 1x "
         "bitmap. 3 leaves 2x to spare at a 1.5x push.")
args = ap.parse_args()

BASE = args.url
TOKEN = args.token
OUT = pathlib.Path(args.out)
EXE = chromium()
W, H = args.width, args.height

STAGE = r"""([W, H]) => {
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
    setTimeout(() => { cursor.style.transition = ""; place(); }, 120);
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
  const apply = () => {
    shell.style.transform = `translate(${cam.tx}px, ${cam.ty}px) scale(${cam.s})`;
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
            self.page.wait_for_timeout(ms / frames)
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
        self.page.wait_for_timeout(330)
        self.page.mouse.down()
        self.page.wait_for_timeout(85)
        self.page.mouse.up()
        self.page.wait_for_timeout(settle)


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
        self.page.wait_for_timeout(ms)

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

    def line(self, text, hold):
        """A caption, held, then cleared."""
        self.say(text)
        self.beat(hold)
        self.hush()
        self.beat(450)


def film_full(page):
    """The long shot list. Re-cutting means editing this, nothing else."""
    reel = Reel(page)
    hand = reel.hand

    reel.title("Bmail", "Email triage that runs on your own laptop")
    reel.beat(2700)
    reel.drop()
    reel.beat(1100)

    reel.line("Right \u2014 this is everything that needs me today.", 3200)
    reel.beat(150)

    reel.push(".stack > .card", 1.5, 1900, 1)
    reel.beat(500)
    reel.say("It pulled that deadline out of the email, and it shows me the line it got it from.")
    hand.at(".quote", 1, ms=900)
    reel.beat(3400)
    reel.hush()
    reel.beat(400)
    reel.pull(1700)
    reel.beat(800)

    hand.click('[data-tab="cases"]', ms=900, settle=1400)
    reel.push(".stack > .card", 1.4, 1900, 1)
    reel.beat(500)
    reel.line("Every application I've sent, and where each one actually got to.", 3200)

    hand.click(".stack > .card", 1, ms=800, settle=1500)
    reel.push(".aside", 1.45, 1800)
    reel.beat(500)
    reel.line(
        "Applied, acknowledged, then nothing for three weeks. That's the bit I lose track of.",
        3600,
    )
    reel.pull(1700)
    reel.beat(800)

    hand.click('[data-tab="mail"]', ms=900, settle=1400)
    hand.click('[data-list="mail"] .row', 4, ms=800, settle=1500)
    reel.push(".aside", 1.5, 1900)
    reel.beat(500)
    reel.say("If I want to know why it decided that, it's all here.")
    reel.beat(3200)
    hand.at(".aside select", ms=700)
    reel.beat(700)
    reel.hush()
    reel.beat(300)
    reel.line("And if it's wrong I fix it here, and it remembers.", 2900)
    reel.pull(1700)
    reel.beat(800)

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
    reel.say("Or tighter, if I'd rather see more at once.")
    reel.beat(1700)
    reel.theme("compact", 2800)
    reel.theme("studio", 1900)
    reel.hush()
    reel.beat(800)

    reel.title("Nothing leaves the laptop.", "No cloud, no API keys, no account.")
    reel.beat(3400)


def film_short(page):
    """Thirty seconds: the three things that are hard to believe without
    seeing them — that it reads a deadline out of the prose, that it tracks
    state across a thread, and that the verdict is inspectable. Everything
    else is in the long cut."""
    reel = Reel(page)
    hand = reel.hand

    reel.title("Bmail", "Email triage that runs on your own laptop")
    reel.beat(2200)
    reel.drop()
    reel.beat(800)

    reel.line("Everything that needs me today.", 2400)

    reel.push(".stack > .card", 1.5, 1400, 1)
    reel.beat(250)
    reel.say("It pulled that deadline out of the email, and shows me the line it came from.")
    hand.at(".quote", 1, ms=750)
    reel.beat(2900)
    reel.hush()
    reel.beat(300)
    reel.pull(1200)
    reel.beat(400)

    hand.click('[data-tab="cases"]', ms=750, settle=1100)
    reel.push(".stack > .card", 1.4, 1400, 1)
    reel.beat(250)
    reel.line("Every application, and where each one actually got to.", 3000)
    reel.pull(1200)
    reel.beat(400)

    hand.click('[data-tab="mail"]', ms=750, settle=1000)
    hand.click('[data-list="mail"] .row', 4, ms=700, settle=1200)
    reel.push(".aside", 1.5, 1400)
    reel.beat(250)
    reel.line("And why it decided that, in full.", 2900)
    reel.pull(1200)
    reel.beat(500)

    reel.title("Nothing leaves the laptop.", "No cloud, no API keys, no account.")
    reel.beat(2800)


with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=EXE, args=["--no-sandbox", "--hide-scrollbars"])
    ctx = browser.new_context(
        viewport={"width": W, "height": H},
        device_scale_factor=args.raster,
        record_video_dir=str(OUT / "raw"),
        record_video_size={"width": W, "height": H},
    )
    page = ctx.new_page()
    page.goto(f"{BASE}/?token={TOKEN}", wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    page.evaluate("localStorage.setItem('bmail.window','3650'); localStorage.setItem('bmail.theme','studio')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    page.wait_for_timeout(1500)
    page.evaluate(STAGE, [W, H])
    page.wait_for_timeout(700)
    (film_short if args.cut == "short" else film_full)(page)
    path = page.video.path()
    ctx.close()
    browser.close()
print(path)
