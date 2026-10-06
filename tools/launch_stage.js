/* The stage for the launch cut: the app as a window in a dark room.
 *
 * Installed by record_film.py after its own stage, for `--cut launch` only. The
 * walkthrough cuts show the page; this one shows the page as an object, so it
 * needs a room to stand in, a camera that can tilt, type big enough to carry a
 * line on its own, and the mark arriving as an event.
 *
 * Still the running app in every frame: the window is #shell itself, moved.
 * Styled through the CSSOM throughout, as the page's style-src demands.
 *
 * The recording is acted out slower than the film runs: the recorder slows
 * the page's animation clock, and with it the time each animation frame is
 * handed. The camera keeps time by that clock and no other, so its moves
 * stretch with everything else. performance.now() does not slow down, and a
 * move timed against it runs away.
 *
 * The same clock is the film's clock. A page busy drawing a large window can
 * take a few tenths of a second of film to hear from the recorder, which is
 * most of a beat. So the recorder does not say "do this now": it says, ahead
 * of time, "do this at second 52.302", and at() below does it on that frame.
 */
([W, H, STAMP]) => {
  const css = (n, p) => { for (const [k, v] of Object.entries(p)) n.style[k] = v; return n; };
  const make = (tag, p, text) => {
    const n = css(document.createElement(tag), p);
    if (text != null) n.textContent = text;
    return n;
  };
  const NS = "http://www.w3.org/2000/svg";
  const SANS = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  const INK = "#07080C", PAPER = "#F4ECDF", TILE = "#10111A";
  const SETTLE = "cubic-bezier(.16, 1, .3, 1)";      // fast in, long settle
  const LAND = "cubic-bezier(.2, 1.35, .4, 1)";      // overshoots, then sits
  const shell = document.querySelector("#shell");

  // ---- the room --------------------------------------------------------------
  // The page's own ground is the colour the sidebar sits on. The room is dark,
  // so the window has to carry that colour itself or the sidebar goes black.
  css(document.documentElement, { background: INK });
  css(document.body, {
    background: "transparent", overflow: "hidden",
    perspective: "2400px", perspectiveOrigin: "50% 46%",
  });
  document.body.append(make("div", {
    position: "fixed", inset: "0", zIndex: "-1", pointerEvents: "none",
    background: [
      "radial-gradient(42% 38% at 20% 26%, rgba(47,85,196,.30), transparent 72%)",
      "radial-gradient(36% 34% at 82% 76%, rgba(244,236,223,.09), transparent 72%)",
      "radial-gradient(120% 92% at 50% 36%, #1A1D2C 0%, #0E1018 46%, #07080C 100%)",
    ].join(","),
  }));
  // A second light, centre stage, that can keep time with the music.
  const glow = make("div", {
    position: "fixed", inset: "0", zIndex: "-1", pointerEvents: "none", opacity: "0",
    background: "radial-gradient(58% 52% at 50% 44%, rgba(96,132,255,.30), transparent 72%)",
  });
  document.body.append(glow);
  let keeping = null;
  css(shell, {
    background: "var(--bg)", borderRadius: "16px", overflow: "hidden",
    boxShadow: "0 0 0 1px rgba(255,255,255,.09), 0 60px 130px -34px rgba(0,0,0,.85), "
             + "0 20px 44px -20px rgba(0,0,0,.6)",
  });
  // Darker at the corners, the way a lens is.
  document.body.append(make("div", {
    position: "fixed", inset: "0", zIndex: "2147481000", pointerEvents: "none",
    background: "radial-gradient(115% 100% at 50% 48%, transparent 58%, rgba(0,0,0,.34) 100%)",
  }));

  // ---- the camera ------------------------------------------------------------
  // A pose is a point in the page's layout (cx, cy), where on screen that point
  // sits (ax, ay), how large (s), and a tilt about that point. Tilting about
  // the thing being looked at keeps it still while the rest of the window swings.
  const cam = { s: 1, cx: W / 2, cy: H / 2, ax: W / 2, ay: H / 2, rx: 0, ry: 0, o: 1 };
  const apply = () => {
    shell.style.transform =
      `translate(${cam.ax - cam.s * cam.cx}px, ${cam.ay - cam.s * cam.cy}px) scale(${cam.s}) `
      + `translate(${cam.cx}px, ${cam.cy}px) rotateX(${cam.rx}deg) rotateY(${cam.ry}deg) `
      + `translate(${-cam.cx}px, ${-cam.cy}px)`;
    shell.style.opacity = String(cam.o);
  };
  const EASE = {
    inout: (t) => (t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
    out: (t) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t)),
    drift: (t) => .5 - Math.cos(Math.PI * t) / 2,
  };
  let move = 0;
  const drive = (target, ms, ease = "inout") => {
    const mine = ++move, from = { ...cam }, to = { ...cam, ...target };
    if (!ms) { Object.assign(cam, to); apply(); return; }
    const start = document.timeline.currentTime;
    const step = (now) => {
      if (mine !== move) return;                     // a newer move took over
      const t = Math.min(1, (now - start) / ms), e = EASE[ease](t);
      for (const k of Object.keys(cam)) cam[k] = from[k] + (to[k] - from[k]) * e;
      apply();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  // Where a thing sits in the layout, whatever the camera is doing: read it
  // with the camera off. Nothing is painted between the two writes.
  const layout = (node) => {
    const held = shell.style.transform;
    shell.style.transform = "none";
    const r = node.getBoundingClientRect();
    shell.style.transform = held;
    return r;
  };

  // ---- type ------------------------------------------------------------------
  const rise = (node, delay, ms = 620, from = 22) => node.animate(
    [{ opacity: 0, transform: `translateY(${from}px)`, filter: "blur(7px)" },
     { opacity: 1, transform: "none", filter: "blur(0)" }],
    { duration: ms, delay, easing: SETTLE, fill: "both" });
  const leave = (node, ms = 380) => node.animate(
    [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-10px)" }],
    { duration: ms, easing: "ease-in", fill: "forwards" });
  const inWords = (node, text, delay = 0, gap = 70) => {
    node.replaceChildren();
    text.split(" ").forEach((word, i) => {
      const span = make("span", { display: "inline-block", whiteSpace: "pre" }, word + " ");
      node.append(span);
      rise(span, delay + i * gap);
    });
    return delay + text.split(" ").length * gap;
  };

  // Centre of the frame: a statement with the room to itself.
  const centre = make("div", {
    position: "fixed", inset: "0", zIndex: "2147482500", pointerEvents: "none",
    display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
    gap: "22px", textAlign: "center",
  });
  const head = make("div", {
    font: `700 92px/1.04 ${SANS}`, letterSpacing: "-.045em", color: "#fff", maxWidth: "17ch",
  });
  const tail = make("div", {
    font: `400 30px/1.4 ${SANS}`, letterSpacing: "-.015em", color: "rgba(255,255,255,.62)",
  });
  centre.append(head, tail);

  // Bottom of the frame: one line under the picture. The scrim is for close
  // shots, where the line would otherwise sit on a white page; over the dark
  // room it is invisible.
  const scrim = make("div", {
    position: "fixed", left: "0", right: "0", bottom: "0", height: "340px",
    zIndex: "2147482000", pointerEvents: "none", opacity: "0",
    background: "linear-gradient(to bottom, transparent, rgba(7,8,12,.86) 62%, rgba(7,8,12,.96))",
    transition: "opacity 420ms ease",
  });
  const foot = make("div", {
    position: "fixed", left: "0", right: "0", bottom: "78px", zIndex: "2147482100",
    pointerEvents: "none", textAlign: "center",
    font: `600 44px/1.15 ${SANS}`, letterSpacing: "-.035em", color: "#fff",
  });

  // ---- the mark, arriving ------------------------------------------------------
  const lockup = make("div", {
    position: "fixed", inset: "0", zIndex: "2147482600", pointerEvents: "none",
    display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
    gap: "30px",
  });
  const row = make("div", { display: "flex", alignItems: "center", gap: "34px" });
  const tile = make("div", {
    width: "172px", height: "172px", borderRadius: "40px", background: TILE, color: PAPER,
    boxShadow: "inset 0 0 0 2px #2C2E3A, 0 40px 90px -30px rgba(0,0,0,.9)",
  });
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 64 64");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("fill-rule", "evenodd");
  css(svg, { width: "100%", height: "100%", display: "block", overflow: "visible" });
  const stamp = document.createElementNS(NS, "g");
  css(stamp, { transformBox: "view-box", transformOrigin: "50% 50%" });
  const path = document.createElementNS(NS, "path");
  path.setAttribute("transform", "rotate(-6 32 32)");
  path.setAttribute("d", STAMP);
  stamp.append(path);
  svg.append(stamp);
  tile.append(svg);
  const name = make("div", {
    font: `700 118px/1 ${SANS}`, letterSpacing: "-.05em", color: "#fff",
  }, "Bmail");
  const under = make("div", {
    font: `400 30px/1.4 ${SANS}`, letterSpacing: "-.015em", color: "rgba(255,255,255,.62)",
  });
  const small = make("div", {
    font: `500 22px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`,
    color: "rgba(255,255,255,.42)", marginTop: "-14px",
  });
  row.append(tile, name);
  lockup.append(row, under, small);

  // ---- a light on one thing ----------------------------------------------------
  const spot = make("div", {
    position: "fixed", zIndex: "2147481500", pointerEvents: "none", opacity: "0",
    borderRadius: "10px", transition: "opacity 520ms ease",
    boxShadow: "0 0 0 2px rgba(47,85,196,.85), 0 0 0 4000px rgba(7,8,12,.5)",
  });

  const black = make("div", {
    position: "fixed", inset: "0", zIndex: "2147483600", pointerEvents: "none",
    background: "#000", opacity: "0",
  });
  document.body.append(spot, scrim, foot, centre, lockup, black);
  lockup.style.visibility = "hidden";

  // ---- appointments ------------------------------------------------------------
  let zero = null;              // second zero of the film, on the slowed clock
  let waiting = false, missed = 0, worst = 0;
  const queue = [];
  const keep = () => {
    const now = document.timeline.currentTime - zero;
    while (queue.length && queue[0].due <= now) {
      const next = queue.shift();
      next.run();
    }
    waiting = queue.length > 0;
    if (waiting) requestAnimationFrame(keep);
  };

  const api = {
    /* Start the film's clock. Returns the wall-clock time it started at, so
       the recorder can set its own clock, and the frames', by this one. */
    roll() { zero = document.timeline.currentTime; return Date.now(); },
    /* Do something at a second of the film. Told too late, do it now and own up. */
    at(second, name, args) {
      const due = second * 1000, now = document.timeline.currentTime - zero;
      if (zero === null || due <= now) {
        if (zero !== null) { missed += 1; worst = Math.max(worst, now - due); }
        api[name](...args);
        return;
      }
      let slot = queue.length;
      while (slot > 0 && queue[slot - 1].due > due) slot -= 1;   // after anything due no later
      queue.splice(slot, 0, { due, run: () => api[name](...args) });
      if (!waiting) { waiting = true; requestAnimationFrame(keep); }
    },
    report() { return { missed, worst: Math.round(worst), pending: queue.length }; },
    pointer(on) { window.__film.pointer(on); },
    /* The app's own switch, the one Setup's Colours control throws. */
    scheme(name) { applyScheme(name); },
    /* Press what the pointer has been brought to. The pointer is moved by the
       recorder, which is how the page comes to show its hover; only the press
       has to be on the beat, so only the press is made here. */
    press(sel, index) {
      const node = document.querySelectorAll(sel)[index || 0];
      if (!node) return;
      window.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      node.click();
    },
    pose(target, ms, ease) { drive(target, ms, ease); },
    /* Bring one thing to the middle of the frame, square on. A thing that was
       asked for a moment ago may not be on the page yet; give it a few frames. */
    focus(sel, index, s, ms, ay = H * .45, tries = 90) {
      const node = document.querySelectorAll(sel)[index || 0];
      if (!node) {
        if (tries) requestAnimationFrame(() => api.focus(sel, index, s, ms, ay, tries - 1));
        return;
      }
      const r = layout(node);
      // Close enough to fill the frame, the window must go on filling it: stop
      // short of showing the room down one side.
      const within = (v, lo, hi) => (s < 1 ? v : Math.min(hi, Math.max(lo, v)));
      const cx = within(r.left + r.width / 2, W / 2 / s, W - W / 2 / s);
      const cy = within(r.top + r.height / 2, ay / s, H - (H - ay) / s);
      drive({ s, cx, cy, ax: W / 2, ay, rx: 0, ry: 0 }, ms);
    },
    words(text, sub) {
      centre.getAnimations({ subtree: true }).forEach((a) => a.cancel());
      const end = inWords(head, text);
      tail.textContent = sub || "";
      if (sub) rise(tail, end + 120, 700, 14);
    },
    wordsOut() { leave(head); leave(tail); },
    line(text) {
      foot.getAnimations({ subtree: true }).forEach((a) => a.cancel());
      inWords(foot, text, 0, 55);
      scrim.style.opacity = "1";
    },
    lineOut() { leave(foot, 320); scrim.style.opacity = "0"; },
    logo(tagline, address) {
      lockup.getAnimations({ subtree: true }).forEach((a) => a.cancel());
      under.textContent = tagline || "";
      small.textContent = address || "";
      lockup.style.visibility = "visible";
      tile.animate(
        [{ opacity: 0, transform: "scale(.72)" }, { opacity: 1, transform: "none" }],
        { duration: 560, easing: SETTLE, fill: "both" });
      // The stamp comes down from above, crooked, and is pressed flat.
      stamp.animate(
        [{ opacity: 0, transform: "translateY(-26px) scale(1.55) rotate(13deg)" },
         { opacity: 1, transform: "none" }],
        { duration: 520, delay: 300, easing: LAND, fill: "both" });
      tile.animate(
        [{ transform: "none" }, { transform: "scale(.955)" }, { transform: "none" }],
        { duration: 240, delay: 640, easing: "ease-out" });
      name.animate(
        [{ opacity: 0, transform: "translateX(-28px)", filter: "blur(8px)" },
         { opacity: 1, transform: "none", filter: "blur(0)" }],
        { duration: 700, delay: 760, easing: SETTLE, fill: "both" });
      rise(under, 1150, 700, 14);
      rise(small, 1500, 700, 10);
    },
    logoOut() {
      lockup.animate(
        [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "scale(1.03)" }],
        { duration: 420, easing: "ease-in", fill: "forwards" })
        .finished.then((a) => { lockup.style.visibility = "hidden"; a.cancel(); });
    },
    /* Light one thing where it stands on screen. Square-on shots only. */
    spot(sel, index, pad = 10) {
      const node = document.querySelectorAll(sel)[index || 0];
      if (!node) return;
      const r = node.getBoundingClientRect();
      css(spot, {
        left: `${r.left - pad}px`, top: `${r.top - pad}px`,
        width: `${r.width + 2 * pad}px`, height: `${r.height + 2 * pad}px`, opacity: "1",
      });
    },
    unspot() { spot.style.opacity = "0"; },
    /* Swell the light once every `ms`, starting now; 0 puts it out. Call it on
       a downbeat and it stays on the beat: it runs on the same slowed clock. */
    pulse(ms) {
      if (keeping) keeping.cancel();
      keeping = ms ? glow.animate(
        [{ opacity: 0 }, { opacity: 1, offset: .07 }, { opacity: 0 }],
        { duration: ms, iterations: Infinity, easing: "ease-out" }) : null;
    },
    fade(ms) {
      black.animate([{ opacity: 0 }, { opacity: 1 }], { duration: ms, easing: "ease-in", fill: "forwards" });
    },
  };
  window.__cine = api;
}
