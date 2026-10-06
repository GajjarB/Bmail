"""What only the launch cut needs: its stage controls, and a way to keep its picture.

Used by record_film.py. On its own it does nothing.
"""
import base64
import json
import time


class Frames:
    """The picture, kept as the frames Chromium hands over and when each came.

    Playwright's recorder takes these same frames and squeezes them into VP8 at
    a megabit a second. That is enough for a white page and not for a dark
    room: the gradients band and the type smears. Kept as JPEGs with their
    timestamps they can be encoded once, properly, by ffmpeg.

    Chromium only sends a frame when the picture changes, so a held shot is one
    frame with a long duration, not a second of identical ones.

    The film is acted out `slow` times slower than it runs (see --slow), so
    every time written down here is divided by that on its way out.
    """

    def __init__(self, context, page, out, size, slow=1.0):
        self.out = out
        self.size = size
        self.slow = slow
        self.folder = out / "frames"
        self.folder.mkdir(parents=True, exist_ok=True)
        for old in self.folder.glob("*.jpg"):
            old.unlink()
        self.began = None         # when the film's clock started, seconds since the epoch
        self.stamps = []          # when each frame arrived, on the same clock
        self.cues = {}            # moments the score is written to, in seconds of film
        self.cdp = context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._keep)
        # The one switch that slows the page: transitions, animations, and
        # the time handed to each animation frame, which the camera keeps to.
        self.cdp.send("Animation.enable")
        self.cdp.send("Animation.setPlaybackRate", {"playbackRate": 1 / slow})

    def start(self):
        """Start keeping frames. Second zero is now, until the stage says
        when its own clock started and `began` is set to that."""
        self.began = time.time()
        width, height = self.size
        self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 95, "maxWidth": width, "maxHeight": height,
            "everyNthFrame": 1,
        })

    def _keep(self, frame):
        name = self.folder / f"{len(self.stamps):06d}.jpg"
        name.write_bytes(base64.b64decode(frame["data"]))
        self.stamps.append(frame["metadata"]["timestamp"])
        self.cdp.send("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})

    def mark(self, kind, second):
        """Note a second of the film as a moment the score should land on."""
        self.cues.setdefault(kind, []).append(round(second, 3))

    def finish(self):
        """Stop, and write the frame list ffmpeg reads and the cue sheet."""
        self.cdp.send("Page.stopScreencast")
        end = time.time()
        if not self.stamps:
            raise SystemExit("Chromium sent no frames.")
        # Second zero is when the clock started, not when the first frame came:
        # a film cut to music has to begin where the shot list says it does.
        start = self.began
        lines = ["ffconcat version 1.0"]
        for index, stamp in enumerate(self.stamps):
            after = max(self.stamps[index + 1] if index + 1 < len(self.stamps) else end, start)
            # Frames from before second zero collapse onto it; the last of
            # them fills whatever gap there is before the next.
            stamp = max(stamp, start)
            lines += [f"file 'frames/{index:06d}.jpg'",
                      f"duration {max((after - stamp) / self.slow, 0.0005):.5f}"]
        # The concat demuxer drops the last duration unless the file is named again.
        lines.append(f"file 'frames/{len(self.stamps) - 1:06d}.jpg'")
        (self.out / "frames.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        sheet = dict(self.cues)
        sheet["length"] = round((end - start) / self.slow, 3)
        (self.out / "cues.json").write_text(json.dumps(sheet, indent=2) + "\n", encoding="utf-8")
        return self.out / "frames.txt"


class Cine:
    """The launch cut's stage: the room, the tilting camera, the big type.

    Nothing here waits. A camera move and a line of type are started and left
    running so they can overlap; the shot list does its holding itself.

    `at(second)` makes the next call an appointment: the stage is told now and
    acts at that second of the film, on its own clock. Told ahead of time, it
    is on the frame; told at the moment, it is as late as the page was busy."""

    def __init__(self, page):
        self.page = page
        self.due = None

    def at(self, second):
        self.due = second
        return self

    def _do(self, name, *args):
        due, self.due = self.due, None
        if due is None:
            self.page.evaluate("a => { window.__cine[a[0]](...a.slice(1)); }", [name, *args])
        else:
            self.page.evaluate("a => { window.__cine.at(a[0], a[1], a.slice(2)); }",
                               [due, name, *args])

    def roll(self):
        """Start the film's clock; the wall-clock second it started at."""
        return self.page.evaluate("() => window.__cine.roll()") / 1000

    def report(self):
        return self.page.evaluate("() => window.__cine.report()")

    def pointer(self, shown):
        self._do("pointer", shown)

    def scheme(self, name):
        """Light or dark, by the app's own switch."""
        self._do("scheme", name)

    def press(self, selector, index=0):
        """Press what the pointer is over."""
        self._do("press", selector, index)

    def pose(self, ms=0, ease="inout", **target):
        """Move the camera to a pose; only the parts named change."""
        self._do("pose", target, ms, ease)

    def focus(self, selector, scale, ms, index=0):
        """Square on to one thing, in the middle of the frame."""
        self._do("focus", selector, index, scale, ms)

    def words(self, text, sub=""):
        self._do("words", text, sub)

    def words_out(self):
        self._do("wordsOut")

    def line(self, text):
        self._do("line", text)

    def line_out(self):
        self._do("lineOut")

    def logo(self, tagline, address=""):
        self._do("logo", tagline, address)

    def logo_out(self):
        self._do("logoOut")

    def spot(self, selector, index=0, pad=10):
        self._do("spot", selector, index, pad)

    def unspot(self):
        self._do("unspot")

    def pulse(self, ms):
        """A light that swells once every `ms`, from now; 0 puts it out."""
        self._do("pulse", ms)

    def fade(self, ms):
        self._do("fade", ms)
