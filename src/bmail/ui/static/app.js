/* Bmail — the desktop application.
 *
 * Framework-free on purpose: the page is served by the local daemon and must
 * work with the machine offline, so there is no CDN, no bundler and no build
 * step. The file you read is the file that runs.
 *
 * Two rules run through the whole file:
 *   - a screen is a list of records, dense enough to scan, and every label is
 *     as short as it can be while still being true;
 *   - nothing is ever inserted as markup, so mail content cannot become HTML.
 */

"use strict";

/* Two skins over one set of markup. "studio" is the roomy, panelled idiom;
 * "compact" is the dense mail-client one. Applied before anything is drawn —
 * the shell stays hidden until boot finishes, so neither one flashes. */
const THEMES = { studio: "Studio", compact: "Compact" };

function applyTheme(name) {
  const theme = THEMES[name] ? name : "studio";
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem("bmail.theme", theme); } catch { /* private window */ }
  return theme;
}

applyTheme((() => {
  try { return localStorage.getItem("bmail.theme"); } catch { return null; }
})());

const App = {
  token: null,
  state: null,
  route: "today",
  poll: null,
  reviewIndex: 0,
  packChoice: null,   // the pack the user picked, or null for the configured one
  cursor: -1,         // keyboard position in the current list, -1 for "nowhere"
  overlay: null,      // the open sheet, if any, and how to close it
  drawer: null,       // the detail drawer over the board, if open
  mailKey: null,      // the mail list on screen, so opening a message keeps it
  skeleton: false,    // a placeholder is on screen, so skip the entrance again
  arriving: false,    // this paint is a new screen, not a redraw of this one
};

/* ----------------------------------------------------------------- dom --- */

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;   // never innerHTML
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat(3)) {
    if (kid === null || kid === undefined || kid === false || kid === "") continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

/* Icons are Lucide (ISC), vendored in icons.js as [element, attributes] lists
 * on a 24px grid. Built as real SVG nodes, not markup, for the same reason
 * nothing else here is: the el() rule holds even for our own assets. */
const SVG_NS = "http://www.w3.org/2000/svg";

function icon(name, size = 16, stroke = 1.75) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", stroke);
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "icon");
  for (const [tag, attrs] of (window.BMAIL_ICONS || {})[name] || []) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    svg.append(node);
  }
  return svg;
}

/* The mark: the same three lines as the favicon, so the tab and the window
 * agree about what this is. */
function mark() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2.4");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M5 7.5h14M5 12h14M5 16.5h8");
  svg.append(path);
  return svg;
}

const REDUCED = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------------------------------------------------------- formatting --- */

const DAY = 86400000;

function daysFrom(value) {
  if (!value) return null;
  const then = new Date(value);
  if (Number.isNaN(then.getTime())) return null;
  const a = new Date(); a.setHours(0, 0, 0, 0);
  const b = new Date(then); b.setHours(0, 0, 0, 0);
  return Math.round((b - a) / DAY);
}

/** "in 3 days" / "yesterday" / "3 weeks ago" — how a person would say it. */
/* For something that already happened. Mail is occasionally dated in the
 * future — clock skew, or a sender that sets Date badly — and "arrived in three
 * weeks" is nonsense to read, so the future is clamped to the present. */
function agoWords(value) {
  const days = daysFrom(value);
  if (days === null) return "";
  return days > 0 ? "just now" : whenWords(value);
}

function overdueWords(value) {
  const days = daysFrom(value);
  if (days === null) return "";
  if (days < 0) {
    const ago = -days;
    return ago < 14 ? `${ago} days overdue` : `${Math.round(ago / 7)} weeks overdue`;
  }
  return `due ${whenWords(value)}`;
}

function whenWords(value) {
  const days = daysFrom(value);
  if (days === null) return "";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  if (days > 0) return days < 14 ? `in ${days} days` : `in ${Math.round(days / 7)} weeks`;
  const ago = -days;
  if (ago < 14) return `${ago} days ago`;
  if (ago < 60) return `${Math.round(ago / 7)} weeks ago`;
  return `${Math.round(ago / 30)} months ago`;
}

function dateWords(value, withTime = false) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 10);
  return d.toLocaleString(undefined, withTime
    ? { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
    : { day: "numeric", month: "short", year: "numeric" });
}

const humanise = (value) => String(value || "").replace(/_/g, " ");

const initials = (name, address) => {
  const source = (name || address || "?").trim();
  const parts = source.split(/[\s.@_-]+/).filter(Boolean);
  return ((parts[0] || "?")[0] + (parts.length > 1 ? parts[1][0] : "")).slice(0, 2);
};

/* A sender's colour: stable for a name, one of eight soft hues. Content gets
   colour; the chrome around it stays grey. */
function hueOf(text) {
  let hash = 0;
  for (const ch of String(text || "?").toLowerCase()) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return String(hash % 8);
}

const avatar = (name, address, size = "") => el("span", {
  class: `avatar${size ? ` ${size}` : ""}`, "data-hue": hueOf(address || name),
  "aria-hidden": "true", text: initials(name, address).toUpperCase(),
});

/* The kind of a message, in the same colour wherever it appears. */
const CATEGORY_HUE = {
  interview_invite: 1, assessment: 6, offer: 5, document_request: 3, scheduling: 0,
  recruiter_outreach: 2, rejection: 4, acknowledgement: 7, job_alert: 0, newsletter: 7,
  admin: 7, personal: 2, other: 7,
};
const kindChip = (category) => (category ? el("span", {
  class: "chip", "data-hue": String(CATEGORY_HUE[category] ?? hueOf(category)),
  text: capital(humanise(category)),
}) : null);

const capital = (text) => (text ? text[0].toUpperCase() + text.slice(1) : "");

/** One vocabulary for urgency, used by every dot, chip and card edge. */
function toneFor({ due, urgency, actionRequired, state, stalled, closed }) {
  if (stalled) return "quiet";
  if (closed) return "done";
  const days = due != null ? daysFrom(due) : null;
  if (days !== null && days <= 0) return "urgent";
  if (urgency === "today") return "urgent";
  if (days !== null && days <= 7) return "soon";
  if (urgency === "this_week") return "soon";
  if (actionRequired) return "soon";
  if (state) return "live";
  return "";
}

/* ----------------------------------------------------------------- api --- */

/* Every request carries the chosen pack.
 *
 * Threaded here rather than at each call site: when only /state carried it, the
 * sidebar relabelled itself for the new domain while every list underneath kept
 * showing the old one's data — a bug that looks like a working pack switcher. */
function withPack(path) {
  if (!App.packChoice) return path;
  const [base, rawQuery] = path.split("?");
  const params = new URLSearchParams(rawQuery || "");
  if (!params.has("pack")) params.set("pack", App.packChoice);
  return `${base}?${params}`;
}

async function api(path, options = {}) {
  const response = await fetch(`/api${withPack(path)}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${App.token}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  let payload = null;
  try { payload = await response.json(); } catch { /* empty */ }
  if (!response.ok) throw new Error((payload && payload.error) || `HTTP ${response.status}`);
  return payload;
}
const post = (path, body) =>
  api(path, {
    method: "POST",
    body: JSON.stringify({ ...(App.packChoice ? { pack: App.packChoice } : {}), ...(body || {}) }),
  });

function toast(message, isError = false, action = null) {
  const node = $("#toast");
  const hide = () => {
    node.classList.add("leaving");
    toast.gone = setTimeout(() => { node.hidden = true; }, 220);
  };
  node.replaceChildren(icon(isError ? "alert" : "check", 15, 2), el("span", { text: message }),
    action ? el("button", { class: "toast-action", onclick: () => {
      clearTimeout(toast.timer);
      hide();
      action.run();
    } }, action.label) : null);
  node.classList.toggle("error", Boolean(isError));
  node.classList.remove("leaving");
  node.hidden = false;
  // Restart the entrance even when a toast is already up, so a second message
  // reads as a new event rather than as text quietly changing underneath you.
  node.style.animation = "none";
  void node.offsetWidth;
  node.style.animation = "";
  clearTimeout(toast.timer);
  clearTimeout(toast.gone);
  // Something to do stays long enough to do it.
  toast.timer = setTimeout(hide, isError || action ? 8000 : 3600);
}

/* ---------------------------------------------------------------- jobs --- */

async function run(kind, body, label) {
  try {
    const job = await post(`/${kind}`, body);
    paintJob(job, label);
    watchJob(job.id, label);
  } catch (err) { toast(err.message, true); }
}

function paintJob(job, label) {
  const bar = $("#job");
  const running = Boolean(job) && !["done", "error", "cancelled"].includes(job.status);
  // The sync icon turns for as long as work is happening, whichever button
  // started it. A spinner that stops before the work does is worse than none.
  $("#btn-sync").classList.toggle("busy", running);
  if (!running) { bar.hidden = true; return; }
  bar.hidden = false;
  // Checking for mail sorts what arrived in the same job; the label follows
  // the stage it is in, so a long sort does not read as a slow download.
  const sorting = job.kind === "ingest" && /^(sorting|training)/.test(job.step || "");
  $("#job-label").textContent = sorting ? "Sorting new mail" : (label || job.kind);
  $("#job-fill").style.width = `${Math.round((job.progress || 0) * 100)}%`;
  $("#job-cancel").onclick = () => post(`/jobs/${job.id}/cancel`).catch(() => {});
}

function watchJob(jobId, label) {
  clearInterval(App.poll);
  App.poll = setInterval(async () => {
    let job;
    try { job = await api(`/jobs/${jobId}`); }
    catch { clearInterval(App.poll); App.poll = null; return; }
    paintJob(job, label);
    if (!["done", "error", "cancelled"].includes(job.status)) return;
    clearInterval(App.poll);
    App.poll = null;
    if (job.status === "error") toast(job.error, true);
    else if (job.status === "cancelled") toast("Stopped.");
    else toast(jobWords(job), Object.keys(job.result?.errors || {}).length > 0);
    await refresh();
  }, 500);
}

function jobWords(job) {
  const result = job.result || {};
  if (job.kind === "triage") {
    return result.processed ? `Sorted ${result.processed} messages.` : "Nothing new to sort.";
  }
  if (job.kind === "ingest") return mailWords(result);
  if (job.kind === "export") return `Written to ${result.directory}`;
  return "Done.";
}

function mailWords(result) {
  const sum = (counts) => Object.values(counts || {}).reduce((a, b) => a + b, 0);
  // Older servers reported only `fetched`; everything since reports `new`.
  const fresh = result.new ? sum(result.new) : sum(result.fetched);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  let words = fresh ? `${plural(fresh, "new message")}` : "No new mail";
  if (fresh && result.sorted) words += result.sorted >= fresh ? ", sorted" : `, ${result.sorted} sorted`;
  words += ".";
  const failed = Object.keys(result.errors || {});
  if (failed.length) words += ` Couldn't check ${failed.join(", ")}.`;
  return words;
}

/* ---------------------------------------------------------------- sync --- */
/* While the app is open the engine checks for mail on its own — on a timer,
 * and for IMAP servers that support it, the moment the server says mail
 * landed. The page asks how that is going every 15 seconds (every 2 while a
 * check is running) and redraws when the answer says something arrived. */

const PROVIDERS = { imap: "IMAP", gmail: "Gmail", graph: "Outlook", outlook: "Outlook",
                    microsoft: "Outlook", m365: "Outlook" };

const everyWords = (minutes) => (minutes === 60 ? "hour" : `${minutes} min`);

function ago(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  const seconds = (Date.now() - then) / 1000;
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  const time = then.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return then.toDateString() === new Date().toDateString()
    ? `at ${time}` : `${then.toLocaleDateString()} ${time}`;
}

const Sync = {
  status: null,
  generation: null,
  timer: null,

  async poll() {
    clearTimeout(this.timer);
    let s = null;
    try { s = await api("/sync"); } catch { /* the next poll tries again */ }
    if (s) {
      const arrived = this.generation !== null && s.generation > this.generation;
      this.generation = s.generation;
      this.status = s;
      this.paint();
      // A check someone started by hand reports through its own job; only
      // the ones that happened on their own need announcing here. They never
      // redraw what you are reading: the counts change where they are, and
      // the new mail is one click away. A screen with nothing on it yet (the
      // first import filling an empty Today) has nothing to lose, so it fills.
      if (arrived && s.last_kind === "sync" && App.state) {
        const wasEmpty = !App.state.messages.total;
        await loadState().catch(() => {});
        if (wasEmpty || App.route === "welcome") {
          const typing = document.activeElement?.matches("input, textarea, select");
          if (!typing) await render().catch(() => {});
        } else {
          toast(mailWords({ new: { all: s.last.new }, sorted: s.last.sorted }), false,
            { label: "Show", run: () => render() });
        }
      }
    }
    if (!document.hidden) {
      this.timer = setTimeout(() => this.poll(), s && s.checking ? 2000 : 15000);
    }
  },

  paint() {
    const s = this.status;
    const line = $("#sync-line");
    if (!s || !s.accounts.length) { line.hidden = true; this.paintAccounts(); return; }
    // The icon turns for a background check too, unless a job someone
    // started is already driving it.
    if (!App.poll) $("#btn-sync").classList.toggle("busy", Boolean(s.checking));
    const trouble = s.accounts.find((a) => a.status === "sign-in")
      || s.accounts.find((a) => a.status === "error");
    const live = s.accounts.some((a) => a.mode === "push");
    let text;
    let tone = "";
    if (!s.enabled) text = "Automatic checks off";
    else if (s.checking) text = "Checking…";
    else if (trouble) {
      text = trouble.status === "sign-in"
        ? `${trouble.name} needs you to sign in` : `Couldn't check ${trouble.name}`;
      tone = "soon";
    } else text = s.last_run ? `Checked ${ago(s.last_run)}` : `Checks every ${everyWords(s.interval)}`;
    if (s.enabled && !trouble && live) tone = "live";
    line.hidden = false;
    line.dataset.tone = tone;
    line.title = trouble ? trouble.error
      : live ? "Your mail server tells Bmail the moment something arrives." : "";
    line.replaceChildren(el("span", { class: "pulse", "aria-hidden": "true" }), el("span", { text }));
    this.paintAccounts();
  },

  paintAccounts(list = $("#sync-accounts")) {
    if (!list) return;
    const s = this.status;
    if (!s || !s.accounts.length) {
      list.replaceChildren(el("li", { class: "meta" },
        "No mailbox connected yet. Add one and Bmail keeps it current while it's open."));
      return;
    }
    const known = Object.fromEntries(((App.state && App.state.configured_accounts) || [])
      .map((a) => [a.name, a]));
    list.replaceChildren(...s.accounts.map((a) => {
      let words;
      let tone = "";
      if (a.status === "sign-in") { words = "Needs you to sign in — press Check for new mail"; tone = "soon"; }
      else if (a.status === "error") { words = `Couldn't check: ${a.error}`; tone = "urgent"; }
      else if (a.mode === "push") { words = "Live — new mail shows up within seconds"; tone = "live"; }
      else if (a.mode === "poll") {
        words = `Every ${everyWords(s.interval)}${a.last_ok ? ` · checked ${ago(a.last_ok)}` : ""}`;
      } else words = "Only when you check";
      const entry = known[a.name] || {};
      const remove = entry.removable
        ? el("button", { class: "quiet sm", "aria-label": `Remove ${a.name}`, onclick: async () => {
            const sure = await confirmSheet(`Stop reading ${a.name}?`,
              "Mail already read stays in Bmail. The password is removed from your keychain.",
              "Remove mailbox");
            if (!sure) return;
            try {
              await post(`/accounts/${encodeURIComponent(a.name)}/remove`, {});
              toast(`${a.name} removed. Its mail stays.`);
              await refresh();
              Sync.poll();
            } catch (err) { toast(err.message, true); }
          } }, "Remove")
        : null;
      return el("li", { class: "sync-acct", "data-tone": tone },
        el("span", { class: "pulse", "aria-hidden": "true" }),
        el("b", { text: a.name }),
        el("span", { class: "meta", text: entry.label || PROVIDERS[a.provider] || a.provider }),
        el("span", { class: "sync-state", text: words }),
        remove);
    }));
  },
};

/* --------------------------------------------------------------- views --- */

function paint(...nodes) {
  const main = $("#main");
  const wide = App.route === "mail" ? " wide mailpage" : App.route === "cases" ? " wide" : "";
  // Animate only when a new screen arrives. Selecting a row, running a search
  // or changing a filter redraws the same screen, and replaying the entrance
  // for those makes the whole list flinch every time you click something —
  // motion reporting a change that did not happen.
  const arriving = App.arriving && !App.skeleton;
  // A browser with view transitions cross-fades the content area as one piece.
  // Without them, the page and its rows animate themselves in. Never both: two
  // entrances on top of each other read as a stutter.
  const crossFade = arriving && typeof document.startViewTransition === "function" && !REDUCED();
  const swap = () => {
    main.replaceChildren(el("div", {
      class: `page${wide}${arriving && !crossFade ? "" : " instant"}`,
    }, ...nodes));
    // Scrolling back to the top belongs to arriving somewhere new. Doing it on
    // a redraw throws away the place you had scrolled to in order to click.
    if (arriving) main.scrollTop = 0;
    Keys.rescan();
  };
  App.skeleton = false;
  App.arriving = false;
  if (crossFade) document.startViewTransition(swap);
  else swap();
}

/* A placeholder in the shape of what is loading. Only shown if the daemon takes
   long enough for a blank frame to register as latency — below that it would
   flash, which is worse than the wait it is hiding. */
function paintSkeleton(route) {
  const bar = (cls) => el("span", { class: `skel shimmer${cls ? ` ${cls}` : ""}` });
  const rows = Array.from({ length: route === "mail" ? 12 : 7 }, () =>
    el("div", { class: "skel-row" }, bar(), bar(), bar(), bar()));
  App.skeleton = true;
  const main = $("#main");
  main.replaceChildren(el("div", { class: `page${route === "mail" ? " wide" : ""}` },
    el("div", { class: "skel shimmer skel-title" }),
    ...rows));
}

const empty = (title, body, action, glyph = "empty") =>
  el("div", { class: "empty" },
    el("div", { class: "glyph" }, icon(glyph, 20)),
    el("h3", { text: title }),
    el("p", { text: body }),
    action);

/* --- Today ---------------------------------------------------------- */

async function viewToday() {
  const days = Number(localStorage.getItem("bmail.window") || 7);
  const brief = await api(`/brief?days=${days}&limit=20`);
  const counts = brief.counts || {};
  const state = App.state;

  const today = new Date().toLocaleDateString(undefined,
    { weekday: "long", day: "numeric", month: "long" });
  const hello = el("div", { class: "hello" },
    el("div", {}, el("span", { class: "eyebrow", text: today }), ...headlineFor(counts, state)));

  if (!state.verdicts.total) {
    paint(hello);
    return;
  }

  const main = [];
  if (brief.needs_you.length) {
    const more = (counts.needs_you || 0) - brief.needs_you.length;
    main.push(section("Needs you", counts.needs_you, "today"));
    main.push(el("div", { class: "stack" }, brief.needs_you.map(briefCard)));
    if (more > 0) {
      main.push(el("div", { class: "bar-row" },
        el("button", { class: "sm", onclick: () => go("mail", { action: "1" }) },
          `Show the other ${more}`)));
    }
  } else {
    main.push(section("Needs you", 0, "today"));
    main.push(el("div", { class: "panel panel-pad" },
      empty("All clear", "Nothing is waiting on a reply or an action from you.", null, "clear")));
  }

  if (brief.stalled.length) {
    const worth = brief.stalled.slice(0, 6);
    main.push(section("No reply", brief.stalled.length, "quiet"));
    main.push(el("div", { class: "rows", "data-list": "quiet" },
      worth.map((item) => el("a", { class: "row", href: `#/cases?id=${item.case_id || ""}` },
        avatar(item.title, "", "sm"),
        el("span", { class: "row-main" },
          el("span", { class: "row-title", text: item.title }),
          el("span", { class: "row-sub", text: humanise(item.detail.split("·").pop()) })),
        el("span", { class: "row-side" },
          el("span", { class: "chip", "data-tone": "quiet", text: `${Math.round(item.score)}d silent` }))))));
    if (brief.stalled.length > worth.length) {
      main.push(el("div", { class: "bar-row" },
        el("button", { class: "sm", onclick: () => go("cases", { quiet: "1" }) },
          `Show all ${brief.stalled.length}`)));
    }
  }

  // Needs you is everything still open, whenever it arrived. The period only
  // decides how far back "filed" looks, so its picker lives here, with it.
  main.push(section("Filed for you", counts.archived || 0, "archive"));
  main.push(el("div", { class: "bar-row" },
    el("span", { class: "meta", text: "Newsletters, alerts and automatic acknowledgements." }),
    el("span", { class: "grow" }),
    el("select", { "aria-label": "Filed in", onchange: (e) => {
        localStorage.setItem("bmail.window", e.target.value); render();
      } },
      [[1, "Today"], [7, "Last 7 days"], [30, "Last 30 days"], [3650, "Everything"]]
        .map(([value, label]) => el("option", { value, selected: value === days }, label))),
    el("button", { class: "sm", onclick: () => go("mail") }, "Open")));

  paint(hello, statCards(counts, state, brief),
    el("div", { class: "today-grid" },
      el("div", { class: "today-main" }, main),
      el("aside", { class: "today-side" }, scheduleCard(brief))));
}

const section = (label, count, glyph) =>
  el("h2", { class: "section" },
    glyph ? icon(glyph, 14) : null,
    el("span", { text: label }),
    count != null ? el("span", { class: "n", text: String(count) }) : null);

function greeting() {
  const hour = new Date().getHours();
  const part = hour < 5 || hour >= 18 ? "evening" : hour < 12 ? "morning" : "afternoon";
  const first = (((App.state && App.state.owner) || {}).name || "").trim().split(/\s+/)[0];
  return `Good ${part}${first ? `, ${first}` : ""}`;
}

function headlineFor(counts, state) {
  const needs = counts.needs_you || 0;
  if (!state.verdicts.total) {
    return [
      el("h1", { class: "headline", text: "Nothing read yet" }),
      el("p", { class: "subhead",
        text: "Point Bmail at a mailbox or an mbox file and it will sort it." }),
      el("div", { class: "bar-row" },
        el("button", { class: "primary", onclick: () => go("setup") }, "Connect a mailbox")),
    ];
  }
  const parts = [needs
    ? `${needs} ${needs === 1 ? "thing needs" : "things need"} you`
    : "Nothing is waiting on you"];
  if (counts.deadlines) parts.push(`${counts.deadlines} with a deadline`);
  if (counts.events) parts.push(`${counts.events} in the diary`);
  return [
    el("h1", { class: "headline", text: greeting() }),
    el("p", { class: "subhead", text: `${parts.join(" · ")}.` }),
  ];
}

/* Four numbers worth knowing before anything else, each with the one bar that
   says whether it is a lot. */
function statCards(counts, state, brief) {
  const stat = ({ glyph, hue, title, href, num, sub, fraction }) =>
    el(href ? "a" : "div", { class: "stat", "data-hue": String(hue), href: href || null },
      el("div", { class: "stat-head" },
        el("span", { class: "tile" }, icon(glyph, 14)),
        el("span", { text: title }),
        href ? el("span", { class: "go" }, icon("arrow", 14)) : null),
      el("div", { class: "stat-num", text: String(num) }),
      el("div", { class: "stat-sub", text: sub }),
      el("span", { class: "meter" }, progressFill(fraction || 0)));

  const needs = counts.needs_you || 0;
  const open = counts.open_cases || 0;
  const total = counts.total_cases || 0;
  const dated = [...brief.deadlines.map((d) => d.due), ...brief.events.map((e) => e.when)]
    .filter(Boolean).sort();
  const soon = dated.filter((d) => (daysFrom(d) ?? 99) <= 7).length;
  const filed = counts.archived || 0;
  const handled = filed + needs ? filed / (filed + needs) : 0;
  return el("div", { class: "stats" },
    stat({ glyph: "today", hue: 4, title: "Needs you", href: "#/mail?action=1", num: needs,
      sub: counts.deadlines ? `${counts.deadlines} with a deadline` : "Replies and actions waiting",
      fraction: needs ? (counts.deadlines || 0) / needs : 0 }),
    stat({ glyph: "cases", hue: 0, title: capital(state.entity_plural), href: "#/cases", num: open,
      sub: `${open} open of ${total}`, fraction: total ? open / total : 0 }),
    stat({ glyph: "diary", hue: 1, title: "Coming up", href: null, num: dated.length,
      sub: dated.length ? `Next: ${dateWords(dated[0])}` : "Nothing dated yet",
      fraction: dated.length ? soon / dated.length : 0 }),
    stat({ glyph: "archive", hue: 2, title: "Filed for you", href: "#/mail", num: filed,
      sub: `${Math.round(handled * 100)}% of mail handled for you`, fraction: handled }));
}

/* The month at a glance, with a dot on every day something is due, and the
   next few dated things underneath with how much time is left. */
function scheduleCard(brief) {
  const dayKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-`
    + `${String(d.getDate()).padStart(2, "0")}`;
  const items = [
    ...brief.deadlines.map((d) => ({
      day: String(d.due || ""), title: d.title, kind: "Due", tone: toneFor({ due: d.due }),
      href: `#/cases?id=${d.case_id || ""}`, words: overdueWords(d.due),
    })),
    ...brief.events.map((e) => ({
      day: e.when ? dayKey(new Date(e.when)) : "", title: e.title, kind: "Diary", tone: "live",
      href: `#/cases?id=${e.case_id || ""}`, words: whenWords(e.when),
    })),
  ].filter((item) => item.day).sort((a, b) => a.day.localeCompare(b.day));

  const now = new Date();
  const first = new Date(now.getFullYear(), now.getMonth(), 1);
  const start = new Date(first);
  start.setDate(1 - ((first.getDay() + 6) % 7));   // weeks start on Monday
  const marks = {};
  for (const item of items) if (!marks[item.day] || item.tone === "urgent") marks[item.day] = item.tone;
  let cells = Array.from({ length: 42 }, (_, n) => {
    const d = new Date(start);
    d.setDate(start.getDate() + n);
    return d;
  });
  if (cells.slice(35).every((d) => d.getMonth() !== now.getMonth())) cells = cells.slice(0, 35);

  const calendar = el("div", { class: "calendar", "aria-label": "This month" },
    ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].map((d) => el("span", { class: "dow", text: d })),
    cells.map((d) => el("span", {
      class: `day${d.getMonth() !== now.getMonth() ? " other" : ""}${dayKey(d) === dayKey(now) ? " today" : ""}`,
      "data-tone": marks[dayKey(d)] || null, title: marks[dayKey(d)] ? "Something is due" : null,
      text: String(d.getDate()),
    })));

  const month = now.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const rows = items.slice(0, 5).map((item) => {
    const date = new Date(`${item.day}T12:00:00`);
    const left = daysFrom(item.day) ?? 0;
    return el("a", { class: "due", "data-tone": item.tone, href: item.href },
      el("span", { class: "due-date" },
        el("b", { text: String(date.getDate()) }),
        el("span", { text: date.toLocaleDateString(undefined, { month: "short" }) })),
      el("span", { class: "due-main" },
        el("span", { class: "due-title", text: item.title }),
        el("span", { class: "due-foot" }, el("span", { text: item.kind }), el("span", { text: item.words })),
        // Fuller as it gets closer; a fortnight away starts empty.
        el("span", { class: "meter" }, progressFill(1 - Math.min(Math.max(left, 0), 14) / 14))));
  });
  return el("section", { class: "side-card" },
    el("h3", {}, icon("diary", 15), el("span", { text: "Schedule" }), el("span", { class: "meta", text: month })),
    calendar,
    rows.length ? el("div", { class: "due-list" }, rows)
      : el("p", { class: "meta gap-above", text: "Nothing dated coming up." }));
}

function briefCard(item) {
  const tone = toneFor({ due: item.due, urgency: item.urgency, actionRequired: true });
  const org = [item.org, item.role].filter(Boolean).join(" · ") || item.detail;
  const timing = item.due ? overdueWords(item.due) : item.when ? whenWords(item.when) : "";
  return el("a", {
    class: "card person", "data-tone": tone,
    href: item.message_id ? `#/mail?id=${encodeURIComponent(item.message_id)}` : `#/cases?id=${item.case_id || ""}`,
  },
    avatar(item.org || item.title, ""),
    el("div", { class: "card-top" },
      kindChip(item.category),
      timing ? el("span", { class: "flag when", "data-tone": tone, text: timing }) : null),
    el("div", { class: "card-title", text: item.title }),
    el("div", { class: "card-org", text: org }),
    // The quoted source sentence when a fact was extracted; otherwise what the
    // mail says. A personal note has no date to quote, and a card with only a
    // subject line made you open it to find out what was being asked.
    item.evidence ? el("blockquote", { class: "quote", text: item.evidence })
      : item.detail && item.detail !== org ? el("p", { class: "card-note", text: item.detail })
      : null);
}

/* --- Cases ------------------------------------------------------------ */

async function viewCases(params) {
  const onlyQuiet = params.get("quiet") === "1";
  const stateFilter = params.get("state") || "";
  const query = new URLSearchParams();
  if (stateFilter) query.set("state", stateFilter);
  if (onlyQuiet) query.set("stalled", "1");
  const data = await api(`/cases?${query}`);
  const selected = params.get("id");
  const noun = App.state.entity_plural;

  const controls = el("div", { class: "bar-row" },
    el("select", { onchange: (e) => go("cases", e.target.value ? { state: e.target.value } : {}) },
      el("option", { value: "" }, "Every stage"),
      (data.states || []).map((s) =>
        el("option", { value: s, selected: s === stateFilter }, humanise(s)))),
    el("button", { class: "pill", "aria-pressed": String(onlyQuiet),
      onclick: () => go("cases", onlyQuiet ? {} : { quiet: "1" }) }, "No reply"),
    el("span", { class: "grow" }),
    el("span", { class: "meta", text: `${data.cases.length} ${noun}` }));
  const title = el("div", { class: "hello" }, el("div", {},
    el("h1", { class: "headline", text: capital(noun) }),
    el("p", { class: "subhead", text: `Every ${App.state.entity}, by the stage it has reached.` })));

  if (!data.cases.length) {
    paint(title, controls,
      el("div", { class: "panel panel-pad" }, empty("Nothing tracked yet",
        `A ${App.state.entity} opens the first time Bmail sees mail about one.`,
        el("button", { class: "primary", onclick: () => go("setup") }, "Connect a mailbox"))));
    return;
  }

  // One column per stage that has anything in it, in the pack's own order.
  const byState = {};
  for (const c of data.cases) (byState[c.state] ||= []).push(c);
  const order = [...(data.states || []),
    ...Object.keys(byState).filter((name) => !(data.states || []).includes(name))];
  const recent = (a, b) => (b.stalled ? 1 : 0) - (a.stalled ? 1 : 0)
    || String(b.last_event_at || "").localeCompare(String(a.last_event_at || ""));
  // Every stage on the way is shown, so you can see where an application goes
  // next; one with nothing in it is a slim strip that takes no room from the
  // ones that have. Dead ends (withdrawn, ghosted) appear only when used.
  const detours = ["withdrawn", "ghosted", "written_off", "lapsed"];
  const shown = stateFilter ? [stateFilter]
    : order.filter((name) => byState[name] || !detours.includes(name));
  const columns = shown.map((name) => {
    const cases = (byState[name] || []).sort(recent);
    if (!cases.length) {
      return el("section", { class: "column empty", "data-hue": stageHue(name), title: capital(humanise(name)) },
        el("span", { class: "dot" }),
        el("span", { class: "column-name", text: capital(humanise(name)) }),
        el("span", { class: "n", text: "0" }));
    }
    const closed = cases.every((c) => c.closed);
    return el("section", { class: "column", "data-hue": stageHue(name) },
      el("div", { class: "column-head" },
        el("span", { class: "dot" }),
        el("span", { text: capital(humanise(name)) }),
        el("span", { class: "n", text: String(cases.length) })),
      el("div", { class: `stack${closed ? " closed" : ""}` },
        cases.map((c) => caseCard(c, data.states, selected))));
  });

  paint(title, controls, el("div", { class: "board" }, columns));
  if (selected) {
    const params2 = here();
    delete params2.id;
    openDrawer(await caseAside(selected), () => go("cases", params2));
  }
}

/* A stage's colour: the good outcomes green, the bad ones rose, the waiting
   ones blue and violet, the dead ends grey. */
function stageHue(name) {
  const known = {
    applied: 0, submitted: 0, enquiry: 0, acknowledged: 2, received: 2, assessment: 6,
    interview_scheduled: 1, interviewed: 1, under_review: 1, offer: 5, admitted: 5,
    awarded: 5, paid: 5, rejected: 4, declined: 4, withdrawn: 7, ghosted: 7, lapsed: 7,
  };
  return String(known[name] ?? hueOf(name));
}

/* A detail panel that slides over the board instead of squeezing it. */
function openDrawer(content, onClose) {
  closeDrawer();
  const scrim = el("div", { class: "drawer-scrim", onclick: onClose });
  const panel = el("aside", { class: "drawer", role: "dialog", "aria-label": "Details" }, content);
  document.body.append(scrim, panel);
  App.drawer = { scrim, panel };
}

function closeDrawer() {
  if (!App.drawer) return;
  App.drawer.scrim.remove();
  App.drawer.panel.remove();
  App.drawer = null;
}

function caseCard(c, states, selected) {
  const silent = c.last_inbound_at ? -daysFrom(c.last_inbound_at) : null;
  const tone = toneFor({ stalled: c.stalled, closed: c.closed, due: c.next_action_due, state: c.state });
  // One tag at most: the single thing most worth knowing about it right now.
  let flag = null;
  if (!c.closed && c.next_action_due) {
    flag = el("span", { class: "chip", "data-tone": toneFor({ due: c.next_action_due }),
      text: overdueWords(c.next_action_due) });
  } else if (!c.closed && c.event_datetime) {
    flag = el("span", { class: "chip",
      // An interview that has already happened is history, not a plan.
      "data-tone": (daysFrom(c.event_datetime) ?? -1) >= 0 ? "live" : null,
      text: dateWords(c.event_datetime, true) });
  } else if (c.stalled && silent) {
    flag = el("span", { class: "chip", "data-tone": "quiet", text: `${silent}d silent` });
  }
  return el("a", {
    class: "card", "data-tone": tone, href: `#/cases?id=${c.id}`,
    "aria-selected": String(c.id) === String(selected),
  },
    el("div", { class: "card-who" },
      avatar(c.org || c.title, "", "sm"),
      el("div", { class: "card-title", text: c.org || c.title || "Untitled" })),
    el("div", { class: "card-org",
      text: c.role || `${c.message_count} message${c.message_count === 1 ? "" : "s"}` }),
    flag ? el("div", { class: "card-top" }, flag) : null,
    stageRail(c, states),
    el("div", { class: "card-foot" },
      el("span", {}, icon("mail", 12), ` ${c.message_count}`),
      el("span", { class: "grow" }),
      el("span", { text: c.last_event_at ? agoWords(c.last_event_at) : "" })));
}

/* The state machine, drawn — the thing that makes this a tracker and not a list.
 *
 * Only stages the case actually reached are filled. Inferring them from the
 * current position would claim an interview that never happened, which is
 * exactly the kind of quiet lie that makes a tracker untrustworthy. */
function stageRail(c, states) {
  const detour = ["withdrawn", "ghosted", "written_off"];
  const path = (states || []).filter((s) => !detour.includes(s));
  const reached = new Set(c.reached || []);
  const at = path.indexOf(c.state);
  const off = at === -1;
  const tone = c.stalled ? "quiet" : c.closed ? "done" : "live";

  const pips = path.map((name, i) => el("span", {
    class: "stage",
    "data-done": reached.has(name) && i !== at ? "1" : null,
    "data-now": !off && i === at ? "1" : null,
    "data-tone": tone,
    title: humanise(name),
  }, el("span", { class: "pip" }), i < path.length - 1 ? el("span", { class: "bar" }) : null));

  return el("div", { class: "rail-wrap" },
    el("div", { class: "stages" }, pips),
    el("div", { class: "stage-labels" },
      el("span", { text: off ? `→ ${humanise(c.state)}` : humanise(c.state) })));
}

async function caseAside(caseId) {
  let data;
  try { data = await api(`/cases/${caseId}`); }
  catch (err) { return el("div", { class: "panel panel-pad" }, empty("Not found", err.message, null, "nomatch")); }
  const c = data.case;
  const silent = c.last_inbound_at ? -daysFrom(c.last_inbound_at) : null;

  return el("div", { class: "panel panel-pad" },
    el("div", { class: "eyebrow", text: humanise(c.state) }),
    el("div", { class: "panel-title", text: c.org || c.title }),
    c.role ? el("div", { class: "card-org", text: c.role }) : null,

    el("h3", { text: "Facts" }),
    el("dl", { class: "facts" },
      fact("Opened", dateWords(c.opened_at)),
      fact("Last heard", c.last_inbound_at
        ? `${dateWords(c.last_inbound_at)} · ${agoWords(c.last_inbound_at)}` : "never"),
      silent !== null ? fact("Silent", `${silent} days`) : null,
      c.next_action !== "none" ? fact("Next", humanise(c.next_action)) : null,
      c.next_action_due ? fact("Due", `${c.next_action_due} · ${whenWords(c.next_action_due)}`) : null,
      c.event_datetime ? fact("Diary", dateWords(c.event_datetime, true)) : null,
      c.contact_person ? fact("Contact", c.contact_person) : null),

    el("h3", { text: "History" }),
    el("ul", { class: "timeline" }, (data.timeline || []).map((entry) =>
      el("li", {},
        el("div", { class: "when", text: `${dateWords(entry.date_utc)} · ${humanise(entry.category || "")}` }),
        el("div", { class: "what", text: entry.subject }),
        entry.evidence ? el("blockquote", { class: "quote", text: entry.evidence }) : null))),

    el("button", { class: "quiet sm gap-above", onclick: () => go("cases") }, "Close"));
}

const fact = (key, value) => (value ? [el("dt", { text: key }), el("dd", { text: String(value) })] : null);

/* Set through the CSSOM rather than a style attribute: the page's
   Content-Security-Policy is `style-src 'self'`, which refuses inline styles
   outright — silently, which is how eleven of them survived in here unnoticed. */
function progressFill(fraction) {
  const node = el("span", { class: "fill" });
  node.style.width = `${Math.max(0, Math.min(1, fraction)) * 100}%`;
  return node;
}

/* --- Mail -------------------------------------------------------------- */

async function viewMail(params) {
  const q = params.get("q") || "";
  const category = params.get("category") || "";
  const onlyAction = params.get("action") === "1";
  const offset = Number(params.get("offset") || 0);
  const selected = params.get("id");

  const query = new URLSearchParams({ limit: "60", offset: String(offset) });
  if (q) query.set("q", q);
  if (category) query.set("category", category);
  if (onlyAction) query.set("action", "1");

  // Opening another message changes only the reading panes. Redrawing the list
  // for it would throw away where you had scrolled to in order to click.
  const box = document.querySelector(".mailbox");
  if (!App.arriving && box && App.mailKey === query.toString()) {
    box.querySelectorAll('[data-list="mail"] .row').forEach((row) =>
      row.setAttribute("aria-selected", String(row.dataset.id === selected)));
    box.querySelector(".read-pane").replaceWith(await readPane(selected));
    Keys.rescan();
    return;
  }
  const scrolled = !App.arriving && document.querySelector(".mail-scroll")?.scrollTop;
  const data = await api(`/messages?${query}`);
  App.mailKey = query.toString();

  const search = el("input", {
    type: "search", placeholder: "Search mail", value: q,
    onkeydown: (e) => { if (e.key === "Enter") go("mail", { ...here(), q: e.target.value, offset: 0, id: "" }); },
  });
  const pill = (label, active, onclick) =>
    el("button", { class: "pill", "aria-pressed": String(active), onclick }, label);
  // The kinds worth a single click; every other kind is in the menu beside them.
  const quick = ["interview_invite", "rejection", "job_alert"]
    .filter((name) => (App.state.categories || []).some((c) => c.name === name));
  const pills = el("div", { class: "pills" },
    pill("All", !onlyAction && !category, () => go("mail", q ? { q } : {})),
    pill("Needs me", onlyAction, () => go("mail", { ...here(), action: onlyAction ? "" : "1", offset: 0, id: "" })),
    quick.map((name) => pill(capital(humanise(name)), category === name,
      () => go("mail", { ...here(), category: category === name ? "" : name, offset: 0, id: "" }))),
    el("select", { "aria-label": "Kind",
      onchange: (e) => go("mail", { ...here(), category: e.target.value, offset: 0, id: "" }) },
      el("option", { value: "" }, "All kinds"),
      (App.state.categories || []).map((c) =>
        el("option", { value: c.name, selected: c.name === category }, humanise(c.name)))));

  const list = data.messages.length
    ? el("div", { class: "rows mail-list", "data-list": "mail" },
        data.messages.map((m) => mailRow(m, selected)))
    : el("div", { class: "panel panel-pad" }, empty(q ? "No match" : "No mail yet",
        q ? `Nothing matches “${q}”.` : "Connect a mailbox and Bmail will read it.",
        q ? null : el("button", { class: "primary", onclick: () => go("setup") }, "Connect a mailbox"),
        q ? "nomatch" : "mail"));
  const pager = data.messages.length ? el("div", { class: "bar-row" },
    el("span", { class: "meta", text: `${offset + 1}–${offset + data.messages.length} of ${data.total}` }),
    el("span", { class: "grow" }),
    el("button", { class: "sm", disabled: offset === 0,
      onclick: () => go("mail", { ...here(), offset: Math.max(0, offset - 60), id: "" }) }, "Newer"),
    el("button", { class: "sm", disabled: offset + 60 >= data.total,
      onclick: () => go("mail", { ...here(), offset: offset + 60, id: "" }) }, "Older")) : null;

  paint(el("div", { class: "mailbox" },
    el("section", { class: "mail-col" },
      el("div", { class: "mail-col-head" },
        el("h1", { class: "headline", text: "Mail" }),
        el("label", { class: "field" }, icon("search", 15), search),
        pills),
      el("div", { class: "mail-scroll" }, list, pager)),
    await readPane(selected)));
  if (scrolled) document.querySelector(".mail-scroll").scrollTop = scrolled;
}

function mailRow(m, selected) {
  return el("a", {
    class: "row mail-card", "data-id": m.message_id,
    href: `#/mail?${new URLSearchParams({ ...here(), id: m.message_id })}`,
    "aria-selected": String(m.message_id === selected),
  },
    avatar(m.from_name, m.from_addr),
    el("span", { class: "row-main" },
      el("span", { class: "row-top" },
        el("span", { class: "row-from", text: m.from_name || m.from_addr }),
        el("span", { class: "meta when", text: agoWords(m.date_utc) })),
      el("span", { class: "row-title", text: m.subject || "(no subject)" }),
      el("span", { class: "row-sub", text: m.summary || m.from_addr }),
      kindChip(m.category)));
}

/* The message and what Bmail made of it, side by side. */
async function readPane(messageId) {
  if (!messageId) {
    return el("div", { class: "read-pane solo" },
      el("section", { class: "reader reader-empty" },
        el("div", {}, el("div", { class: "glyph" }, icon("mail", 26)),
          el("p", { text: "Pick a message to read it here." }))));
  }
  let data;
  try { data = await api(`/messages/${encodeURIComponent(messageId)}`); }
  catch (err) {
    return el("div", { class: "read-pane solo" },
      el("section", { class: "reader" }, empty("Not found", err.message, null, "nomatch")));
  }
  const { message: m, verdict: v, case: kase } = data;
  const sure = v ? Math.max(0, Math.min(1, v.confidence || 0)) : 0;

  const reader = el("section", { class: "reader" },
    el("div", { class: "reader-bar" },
      el("span", { text: dateWords(m.date, true) }),
      el("span", { class: "grow" }),
      el("button", { class: "quiet sm", onclick: () => go("mail", { ...here(), id: "" }) },
        icon("close", 14), "Close")),
    el("div", { class: "reader-head" },
      avatar(m.from.name, m.from.address, "lg"),
      el("div", { class: "reader-from", text: m.from.name || m.from.address }),
      el("div", { class: "meta", text: m.from.name ? m.from.address : m.account || "" }),
      el("h1", { class: "reader-subject", text: m.subject || "(no subject)" })),
    m.attachments.length ? el("div", { class: "attachments" }, m.attachments.map((a) =>
      el("span", { class: "attachment" }, icon("download", 14), a.filename))) : null,
    messageText(m));

  const context = el("aside", { class: "aside context" },
    v ? el("section", { class: "side-card" },
      el("h3", { text: "Bmail read this as" }),
      el("div", { class: "pills" },
        kindChip(v.category),
        el("span", { class: "chip", "data-tone": v.action_required ? "urgent" : "quiet",
          text: v.action_required ? "Needs you" : "Filed" })),
      el("div", { class: "confidence" },
        el("span", { text: `${Math.round(sure * 100)}% sure` }),
        el("span", { class: "meter" }, progressFill(sure)),
        el("span", { text: humanise(v.stage) })),
      el("dl", { class: "facts gap-above" },
        v.action_required ? fact("Action", humanise(v.next_action)) : null,
        v.deadline ? fact("Deadline", `${v.deadline} · ${whenWords(v.deadline)}`) : null,
        v.event_datetime ? fact("Diary", dateWords(v.event_datetime, true)) : null,
        m.attachments.length ? fact("Attached", `${m.attachments.length}`) : null)) : null,
    v && v.evidence ? el("section", { class: "side-card" },
      el("h3", {}, icon("quote", 13), v.evidence_ok === false ? " Evidence · not in this email" : " Evidence"),
      el("blockquote", { class: `quote${v.evidence_ok === false ? " bad" : ""}`, text: v.evidence })) : null,
    kase ? el("a", { class: "side-card case-link", href: `#/cases?id=${kase.id}` },
      el("h3", { text: `Part of this ${App.state.entity}` }),
      el("div", { class: "card-who" },
        avatar(kase.org || kase.title, ""),
        el("div", {},
          el("div", { class: "card-title", text: kase.org || kase.title }),
          el("div", { class: "card-org", text: [kase.role, humanise(kase.state)].filter(Boolean).join(" · ") })))) : null,
    v ? el("section", { class: "side-card" },
      el("h3", { text: "Correct it" }),
      correctionControls(messageId, v)) : null);

  return el("div", { class: "read-pane" }, reader, context);
}

// The cleaned text first: what this mail says, without the thread it quotes
// or the footer it carries. The rest is one click away, never gone.
function messageText(m) {
  const short = m.body_text || "";
  const whole = m.body_full || "";
  const text = el("div", { class: "body-text full", text: short || whole || "(no text)" });
  if (!short || !whole) return text;
  let showingWhole = false;
  const toggle = el("button", { class: "quiet sm gap-above", onclick: () => {
    showingWhole = !showingWhole;
    text.textContent = showingWhole ? whole : short;
    toggle.textContent = showingWhole ? "Show only the new part" : "Show the whole message";
  } }, "Show the whole message");
  return el("div", {}, text, toggle);
}

function correctionControls(messageId, verdict) {
  const picker = el("select", {}, (App.state.categories || []).map((c) =>
    el("option", { value: c.name, selected: c.name === verdict.category }, humanise(c.name))));
  const action = el("input", { type: "checkbox", checked: verdict.action_required });
  return el("div", {},
    el("div", { class: "setup-row" }, picker,
      el("label", { class: "check" }, action, " needs me"),
      el("button", { class: "primary sm", onclick: async (event) => {
        // Answer the click before the round trip. On a local daemon the write
        // takes a few milliseconds, but "few" is not "none", and a button that
        // waits to acknowledge you feels broken long before it feels slow.
        const button = event.currentTarget;
        button.disabled = true;
        button.textContent = "Saved";
        try {
          await post(`/correct/${encodeURIComponent(messageId)}`,
            { category: picker.value, action_required: action.checked });
          toast("Saved.");
          await refresh();
        } catch (err) {
          button.disabled = false;
          button.textContent = "Save";
          toast(err.message, true);
        }
      } }, "Save")));
}

/* --- Review ------------------------------------------------------------ */
/* A list of uncertain guesses is a chore. One at a time is a rhythm. */

async function viewReview() {
  const data = await api("/messages?review=1&limit=60");
  const queue = data.messages;

  if (!queue.length) {
    paint(el("h1", { class: "headline", text: "Nothing to check" }),
      el("p", { class: "subhead", text: "Bmail was sure about everything it read." }));
    return;
  }

  App.reviewIndex = Math.min(App.reviewIndex, queue.length - 1);
  const item = queue[App.reviewIndex];
  const detail = await api(`/messages/${encodeURIComponent(item.message_id)}`);
  const { message: m, verdict: v } = detail;
  const done = App.reviewIndex;
  const total = data.total;

  const next = () => { App.reviewIndex += 1; render(); };
  const accept = async (category) => {
    try {
      await post(`/correct/${encodeURIComponent(item.message_id)}`,
        { category, action_required: category === v.category ? v.action_required : undefined });
      App.reviewIndex = 0;
      await refresh();
    } catch (err) { toast(err.message, true); }
  };

  // Every alternative, never a truncated list: the one time it matters is the
  // time the correct answer is the one that got cut off.
  const suggestions = (App.state.categories || []).filter((c) => c.name !== v.category);

  paint(el("div", { class: "review-wrap" },
    el("h1", { class: "headline", text: "Review" }),
    el("p", { class: "subhead",
      text: `${total} ${total === 1 ? "message" : "messages"} Bmail was not sure about.` }),

    el("div", { class: "review-progress gap-above" },
      el("span", { text: `${done + 1} of ${total}` }),
      el("span", { class: "track" }, progressFill((done + 1) / total))),

    el("div", { class: "panel panel-pad gap-above" },
      el("div", { class: "eyebrow", text: `${m.from.name || m.from.address} · ${dateWords(m.date)}` }),
      el("div", { class: "panel-title", text: m.subject || "(no subject)" }),
      el("div", { class: "body-text short gap-above", text: m.body_text || "(no text)" })),

    el("div", { class: "guess" },
      el("div", {},
        el("span", { class: "meta", text: "Bmail read this as " }),
        el("span", { class: "strong", text: humanise(v.category) })),
      el("div", { class: "meta", text: `${Math.round((v.confidence || 0) * 100)}%` })),

    el("div", { class: "setup-row" },
      el("button", { class: "primary", onclick: () => accept(v.category) }, "Yes, that's right"),
      el("button", { class: "quiet", onclick: next }, "Skip")),

    el("h2", { class: "section", text: "Or it is" }),
    el("div", { class: "choices gap-above" }, suggestions.map((c) =>
      el("button", { onclick: () => accept(c.name) }, humanise(c.name))))));
}

/* --- Connecting a mailbox ------------------------------------------------ */
/* One form, used by the first-run wizard and by Setup → Add a mailbox. It
 * recognises the provider from the address as you type, says what that
 * provider needs (usually an app password), and saves nothing until a sign-in
 * has worked. The password leaves this page once, to the engine, which puts it
 * in the system keychain. */

const SINCE_WORDS = { 90: "Last 3 months", 180: "Last 6 months", 365: "Last year", 0: "Everything" };

function detectPreset(address, presets) {
  const domain = (address.split("@")[1] || "").trim().toLowerCase();
  return presets.find((p) => p.domains.includes(domain))
    || { key: "custom", label: domain || "Other", host: domain ? `imap.${domain}` : "",
         port: 993, kind: "imap", password_label: "Password", help: "", help_url: "" };
}

async function connectForm({ onConnected, submitLabel = "Connect" }) {
  const info = await api("/accounts");
  const address = el("input", { type: "email", autocomplete: "email", spellcheck: "false",
    placeholder: "you@example.com", "aria-label": "E-mail address" });
  const password = el("input", { type: "password", autocomplete: "off",
    "aria-label": "Password" });
  const host = el("input", { type: "text", spellcheck: "false", "aria-label": "Server" });
  const port = el("input", { type: "number", min: "1", max: "65535", value: "993",
    "aria-label": "Port" });
  const folder = el("input", { type: "text", value: "INBOX", "aria-label": "Folder" });
  const plain = el("input", { type: "checkbox" });
  const since = el("select", { "aria-label": "Read mail from" },
    info.since_choices.map((d) =>
      el("option", { value: d, selected: d === info.default_since }, SINCE_WORDS[d] || `${d} days`)));
  const provider = el("div", { class: "form-provider" });
  const status = el("p", { class: "form-status", role: "status" });
  const passwordLabel = el("span", { text: "Password" });
  const connect = el("button", { class: "primary", type: "submit", text: submitLabel });
  const check = el("button", { type: "button", text: "Test connection" });
  const secretRow = el("label", { class: "form-row" }, passwordLabel, password);
  let hostTouched = false;
  host.addEventListener("input", () => { hostTouched = true; });

  const say = (text, tone) => {
    status.textContent = text;
    status.dataset.tone = tone || "";
  };

  const refreshPreset = () => {
    const preset = detectPreset(address.value, info.presets);
    const microsoft = preset.kind === "microsoft";
    if (!hostTouched) host.value = preset.host || "";
    passwordLabel.textContent = preset.password_label || "Password";
    secretRow.hidden = microsoft;
    connect.disabled = microsoft || !info.keychain.available;
    check.disabled = microsoft;
    provider.replaceChildren();
    if (!address.value.includes("@")) return;
    // Through el(), which drops empty parts: append() writes null as "null".
    provider.append(...el("div", {},
      el("div", { class: "form-provider-name" },
        el("b", { text: preset.key === "custom" ? "IMAP" : preset.label }),
        preset.host ? el("span", { class: "meta", text: preset.host }) : null),
      preset.help ? el("p", { class: "form-help", text: preset.help }) : null,
      preset.help_url
        ? el("a", { href: preset.help_url, target: "_blank", rel: "noopener noreferrer",
            class: "form-link", text: microsoft ? "How to connect Outlook" : "Where to get it" })
        : null).childNodes);
  };
  address.addEventListener("input", refreshPreset);

  const fields = () => ({
    address: address.value.trim(), password: password.value, host: host.value.trim(),
    port: Number(port.value) || 993, folder: folder.value.trim() || "INBOX",
    since_days: Number(since.value), ...(plain.checked ? { ssl: false } : {}),
  });

  const busy = (on, text) => {
    connect.disabled = on || !info.keychain.available;
    check.disabled = on;
    if (on) say(text, "busy");
  };

  check.onclick = async () => {
    busy(true, "Signing in…");
    try {
      const result = await post("/accounts/check", fields());
      say(`Signed in. ${result.messages.toLocaleString()} messages in ${result.folder}.`, "ok");
    } catch (err) { say(err.message, "error"); }
    finally { busy(false); refreshPreset(); }
  };

  const form = el("form", { class: "connect-form", onsubmit: async (event) => {
    event.preventDefault();
    busy(true, "Signing in…");
    try {
      const result = await post("/accounts", fields());
      password.value = "";   // do not keep it on the page a moment longer
      say(`Connected. ${result.messages.toLocaleString()} messages in ${fields().folder}.`, "ok");
      await onConnected(result);
    } catch (err) {
      say(err.message, "error");
    } finally { busy(false); refreshPreset(); }
  } },
    el("label", { class: "form-row" }, el("span", { text: "E-mail address" }), address),
    provider,
    secretRow,
    el("label", { class: "form-row" }, el("span", { text: "Read mail from" }), since),
    el("details", { class: "form-more" },
      el("summary", { text: "Server settings" }),
      el("label", { class: "form-row" }, el("span", { text: "IMAP server" }), host),
      el("div", { class: "form-pair" },
        el("label", { class: "form-row" }, el("span", { text: "Port" }), port),
        el("label", { class: "form-row" }, el("span", { text: "Folder" }), folder)),
      el("label", { class: "check form-plain" }, plain,
        " No encryption: only for a bridge on this computer, such as Proton Mail Bridge")),
    info.keychain.available ? null
      : el("p", { class: "form-status", "data-tone": "error",
          text: `Bmail keeps passwords in your system keychain, and it cannot reach one: ${info.keychain.hint}` }),
    status,
    el("div", { class: "form-actions" }, connect, check),
    el("p", { class: "meta form-privacy" }, icon("shield", 13),
      " The password goes to your system keychain. Nothing is sent anywhere but your mail server."));
  refreshPreset();
  setTimeout(() => address.focus(), 50);
  return form;
}

function openAddAccount() {
  connectForm({
    submitLabel: "Connect and read",
    onConnected: async (result) => {
      closeOverlay();
      const label = `Reading ${result.account.address}`;
      paintJob(result.job, label);
      watchJob(result.job.id, label);
      Sync.poll();
    },
  }).then((form) => openSheet("Add a mailbox",
    el("div", { class: "sheet-head" },
      el("h2", { text: "Add a mailbox" }),
      el("button", { class: "quiet sm", onclick: closeOverlay, "aria-label": "Close" }, icon("close", 14))),
    el("div", { class: "sheet-body" }, form)))
    .catch((err) => toast(err.message, true));
}

/* --- Welcome (first run) ---------------------------------------------------- */
/* Four steps, one question each: what to track, where the mail is, reading it,
 * and what it found. Shown instead of Today until something has been read or
 * the person skips it; everything here can be changed later in Setup. */

const PACK_WORDS = {
  "job-seeker": ["Job applications", "Interviews, assessments, rejections, and who has gone quiet."],
  "student-admissions": ["University applications", "Deadlines, missing documents and decisions."],
  "hr-recruiter": ["Candidates", "Hiring pipelines, replies and response times."],
  "freelance-ops": ["Invoices", "What is due, what is overdue, and who to chase."],
};

const Wizard = { step: "track", job: null, source: "" };

function wizardSteps(current) {
  const steps = [["track", "What to track"], ["connect", "Your mail"], ["read", "Reading"], ["done", "Ready"]];
  const at = steps.findIndex(([key]) => key === current);
  return el("ol", { class: "wizard-steps", "aria-label": "Setup steps" },
    steps.map(([key, label], index) => el("li", {
      "data-state": index < at ? "done" : index === at ? "now" : null,
      "aria-current": index === at ? "step" : null,
    }, el("span", { class: "wizard-dot" }, index < at ? icon("check", 11) : String(index + 1)),
      el("span", { text: label }))));
}

async function finishSetup() {
  try { await post("/setup", { done: true }); } catch { /* the next start asks again */ }
  Wizard.step = "track";
  Wizard.job = null;
  await refresh();
  Sync.poll();
  location.hash = "#/today";
}

async function startReading(job, source) {
  Wizard.job = job;
  Wizard.source = source;
  Wizard.step = "read";
  render();
}

async function viewWelcome() {
  const s = App.state;
  const page = (...nodes) => paint(el("div", { class: "wizard" }, wizardSteps(Wizard.step), ...nodes));

  if (Wizard.step === "track") {
    const choice = async (pack) => {
      try {
        await post("/setup", { pack });
        localStorage.removeItem("bmail.pack");   // the engine keeps the choice now
        App.packChoice = null;
        Wizard.step = "connect";
        await refresh();
      } catch (err) { toast(err.message, true); }
    };
    return page(
      el("h1", { class: "headline", text: "Let's set up Bmail" }),
      el("p", { class: "subhead",
        text: "It reads your mail on this computer and keeps track of what needs you. Nothing leaves this machine." }),
      el("h2", { class: "wizard-q", text: "What do you want to keep track of?" }),
      // The packs this build knows best first, in a fixed order; any others after.
      el("div", { class: "choice-grid" }, [...(s.packs || [])].sort((a, b) =>
        ((Object.keys(PACK_WORDS).indexOf(a) + 1) || 99)
        - ((Object.keys(PACK_WORDS).indexOf(b) + 1) || 99)).map((pack) => {
        const [title, blurb] = PACK_WORDS[pack] || [humanise(pack), ""];
        return el("button", { class: "choice", "data-pack": pack,
          "aria-pressed": pack === s.pack ? "true" : "false", onclick: () => choice(pack) },
          el("b", { text: title }), blurb ? el("span", { text: blurb }) : null);
      })),
      el("div", { class: "wizard-foot" },
        el("button", { class: "quiet sm", onclick: finishSetup }, "Skip setup")));
  }

  if (Wizard.step === "connect") {
    const form = await connectForm({
      onConnected: (result) => startReading(result.job, result.account.address),
    });
    const path = el("input", { type: "text", class: "grow-field",
      placeholder: "~/Downloads/Takeout/Mail/All mail.mbox", "aria-label": "Export file" });
    return page(
      el("h1", { class: "headline", text: "Connect your mailbox" }),
      el("p", { class: "subhead",
        text: "Bmail signs in to read your mail, and never sends, moves or deletes anything." }),
      el("div", { class: "panel panel-pad" }, form),
      el("h2", { class: "wizard-q", text: "Or start without signing in" }),
      el("div", { class: "wizard-alt" },
        el("div", { class: "wizard-alt-row" },
          el("div", {}, el("b", { text: "Read an export file" }),
            el("p", { class: "meta", text: "An mbox or Maildir, such as a Gmail Takeout export." })),
          el("div", { class: "setup-row" }, path,
            el("button", { onclick: async () => {
              if (!path.value.trim()) return toast("Give me a path first", true);
              try {
                const job = await post("/ingest", { path: path.value.trim(), account: "archive" });
                startReading(job, "the export file");
              } catch (err) { toast(err.message, true); }
            } }, "Read"))),
        el("div", { class: "wizard-alt-row" },
          el("div", {}, el("b", { text: "Try it with sample mail" }),
            el("p", { class: "meta", text: "A made-up job search in English and German, to see how it works first." })),
          el("button", { onclick: async () => {
            try { startReading(await post("/samples", {}), "the sample mail"); }
            catch (err) { toast(err.message, true); }
          } }, "Use sample mail"))),
      el("div", { class: "wizard-foot" },
        el("button", { class: "quiet sm", onclick: () => { Wizard.step = "track"; render(); } },
          icon("back", 13), " Back"),
        el("span", { class: "grow" }),
        el("button", { class: "quiet sm", onclick: finishSetup }, "Skip for now")));
  }

  if (Wizard.step === "read") {
    const fill = el("span", { class: "fill" });
    const stepText = el("p", { class: "meta wizard-progress-step", text: "Starting…" });
    const node = el("div", { class: "wizard-progress" },
      el("span", { class: "track" }, fill), stepText);
    page(
      el("h1", { class: "headline", text: "Reading your mail" }),
      el("p", { class: "subhead", text: `From ${Wizard.source}. This runs on your computer and can take a few minutes.` }),
      node);
    const tick = async () => {
      if (App.route !== "welcome" || Wizard.step !== "read" || !Wizard.job) return;
      let job;
      try { job = await api(`/jobs/${Wizard.job.id}`); } catch { return setTimeout(tick, 1000); }
      fill.style.width = `${Math.round((job.progress || 0) * 100)}%`;
      stepText.textContent = humanise(job.step || "working");
      if (job.status === "done") {
        Wizard.job = job;
        Wizard.step = "done";
        await refresh();
        return;
      }
      if (job.status === "error" || job.status === "cancelled") {
        node.replaceChildren(
          el("p", { class: "form-status", "data-tone": "error",
            text: job.error || "Stopped before it finished." }),
          el("button", { onclick: () => { Wizard.step = "connect"; render(); } }, "Back"));
        return;
      }
      setTimeout(tick, 500);
    };
    tick();
    return undefined;
  }

  // done
  const result = (Wizard.job && Wizard.job.result) || {};
  const read = Object.values(result.new || {}).reduce((a, b) => a + b, 0);
  const needs = s.needs_you || 0;
  return page(
    el("h1", { class: "headline", text: needs
      ? `${needs} ${needs === 1 ? "thing needs" : "things need"} you` : "You're set up" }),
    el("p", { class: "subhead", text: `Read ${read.toLocaleString()} messages and found ${s.cases.total} ${s.entity_plural}.` }),
    el("ul", { class: "wizard-facts" },
      el("li", {}, icon("sync", 14), el("span", { text: (s.configured_accounts || []).length
        ? "New mail is read while Bmail is open. Change how often in Setup."
        : "Connect a mailbox in Setup whenever you like." })),
      el("li", {}, icon("review", 14), el("span", {
        text: "Review asks about anything it was unsure of. Each answer makes it better." })),
      el("li", {}, icon("shield", 14), el("span", {
        text: "Everything stays in the folder shown in Setup. Nothing is uploaded." }))),
    el("div", { class: "wizard-foot" },
      el("button", { class: "primary", onclick: finishSetup }, "Open Today ", icon("arrow", 14))));
}

/* --- Setup -------------------------------------------------------------- */

function viewSetup() {
  const s = App.state;
  const path = el("input", { type: "text", class: "grow-field", placeholder: "~/mail/archive.mbox" });
  const packs = el("select", { onchange: async (e) => {
    // The engine keeps the choice, so background sync sorts with it too.
    try {
      await post("/setup", { pack: e.target.value });
      localStorage.removeItem("bmail.pack");
      // Filters name states of the pack that is leaving; drop them with it.
      location.hash = "#/today";
      location.reload();
    } catch (err) { toast(err.message, true); }
  } }, (s.packs || []).map((p) =>
    el("option", { value: p, selected: p === s.pack }, (PACK_WORDS[p] || [p])[0])));
  const current = Sync.status;
  // Filled now rather than after paint: paint may run inside a view
  // transition, after this function has returned.
  const accounts = el("ul", { class: "sync-accts", id: "sync-accounts" });
  Sync.paintAccounts(accounts);
  // The status in hand may predate an account added a moment ago; ask again
  // and let the list redraw itself when the answer comes.
  Sync.poll();
  const interval = el("select", { "aria-label": "Check automatically", onchange: async (e) => {
    const minutes = Number(e.target.value);
    try {
      Sync.status = await post("/sync", { interval: minutes });
      Sync.paint();
      toast(minutes ? `Checking every ${everyWords(minutes)} while Bmail is open.`
        : "Automatic checks off. Check by hand from the sidebar.");
      // IMAP connections take a moment to go live; ask again shortly.
      setTimeout(() => Sync.poll(), 1500);
    } catch (err) { toast(err.message, true); }
  } }, ((current && current.choices) || [0, 2, 5, 15, 30, 60]).map((n) =>
    el("option", { value: n, selected: Boolean(current) && n === current.interval },
      n ? `Every ${everyWords(n)}` : "Off")));
  const formats = ["xlsx", "csv", "ics"].map((kind) =>
    el("label", { class: "check" },
      el("input", { type: "checkbox", value: kind, checked: kind === "xlsx" }), " ", kind));

  // Label left, control right, a hairline between: a settings list, not a
  // stack of boxes with one control rattling around in each.
  const group = (label, ...children) =>
    el("div", { class: "set-row" },
      el("div", { class: "lbl", text: label }),
      el("div", { class: "setup-row" }, ...children));
  const note = (text) => el("p", { class: "meta set-note", text });

  paint(
    el("h1", { class: "headline", text: "Setup" }),
    el("p", { class: "subhead", text: "Everything here happens on this computer." }),

    section("Mailboxes", null, "mail"),
    accounts,
    el("div", { class: "bar-row" },
      el("button", { class: "primary", onclick: openAddAccount }, "Add a mailbox"),
      el("button", { onclick: () => run("ingest", {}, "Checking for new mail") }, "Check all now")),
    group("Check automatically", interval),
    group("File on this machine", path,
      el("button", { onclick: () => path.value.trim()
        ? run("ingest", { path: path.value.trim(), account: "local" }, "Reading file")
        : toast("Give me a path first", true) }, "Read")),
    note("An mbox, a Maildir or a folder of .eml files. A Gmail Takeout export works."),

    section("Sorting", null, "filter"),
    group("Run",
      el("button", { class: "primary", onclick: () => run("triage", {}, "Sorting new mail") },
        "Sort new mail"),
      el("button", { onclick: () => run("triage", { all: true }, "Sorting everything") },
        "Re-sort everything")),
    note(s.llm.available
      ? (s.llm.pulled
        ? `${s.llm.model}, on this machine, for the hard ones.`
        : `${s.llm.model} is not downloaded — run: ollama pull ${s.llm.model}`)
      : "No local model. Rules and the classifier only, which handles most mail."),

    section("Appearance", null, "palette"),
    group("Layout",
      el("select", { onchange: (e) => { applyTheme(e.target.value); render(); } },
        Object.entries(THEMES).map(([value, label]) =>
          el("option", { value, selected: value === document.documentElement.dataset.theme },
            label)))),
    note("Studio is roomier and easier to read. Compact fits about half again as "
       + "much on a screen, which starts to matter at ninety applications."),

    section("Domain", null, "package"),
    group("Pack", packs,
      el("span", { class: "meta",
        text: `${s.entity_plural} · ${(s.states || []).length} stages` })),

    section("Export", null, "download"),
    group("Formats", formats,
      el("button", { onclick: () => {
        const kinds = formats.map((f) => f.querySelector("input")).filter((i) => i.checked)
          .map((i) => i.value);
        if (!kinds.length) return toast("Pick a format first", true);
        run("export", { kinds }, "Writing files");
      } }, "Write files")),
    note("A spreadsheet, or a calendar of every deadline and interview."),

    section("Storage", null, "storage"),
    el("dl", { class: "facts set-facts" },
      fact("Database", s.database),
      fact("Folder", s.home),
      fact("Messages", `${s.messages.total}`),
      fact("Sorted", `${s.verdicts.total}`),
      fact("Tracked", `${s.cases.total} ${s.entity_plural}`)),

    section("Keys", null, "keys"),
    el("dl", { class: "facts set-facts" },
      [["1", "Today"], ["2", "Cases"], ["3", "Mail"], ["4", "Review"], ["5", "Setup"],
       ["r", "Reload"], ["c", "Check for new mail"], ["s", "Sort new mail"]].map(([key, what]) =>
        [el("dt", {}, el("kbd", { text: key })), el("dd", { text: what })])));
}

/* ------------------------------------------------------------ keyboard --- */
/* A triage tool is used with both hands on the keys. The pointer gets hover;
 * the keyboard gets its own cursor, drawn differently from selection so you can
 * always tell where Enter will land.
 *
 * The rule throughout: never swallow a key the browser or the user needs. Any
 * modifier, or focus inside a field, and this stands down. */

const Keys = {
  items: [],

  rescan() {
    this.items = [...document.querySelectorAll(".page .card, .page .row")];
    if (App.cursor >= this.items.length) App.cursor = this.items.length - 1;
    this.paint();
  },

  paint() {
    this.items.forEach((node, index) => {
      if (index === App.cursor) node.setAttribute("data-cursor", "1");
      else node.removeAttribute("data-cursor");
    });
  },

  move(step) {
    if (!this.items.length) return false;
    const next = App.cursor < 0
      ? (step > 0 ? 0 : this.items.length - 1)
      : App.cursor + step;
    // Stops at the ends rather than wrapping: wrapping in a long list loses
    // your place, and you cannot feel it happen.
    App.cursor = Math.max(0, Math.min(this.items.length - 1, next));
    this.paint();
    this.items[App.cursor].scrollIntoView({ block: "nearest", behavior: REDUCED() ? "auto" : "smooth" });
    return true;
  },

  open() {
    const node = this.items[App.cursor];
    if (node) node.click();
    return Boolean(node);
  },

  reset() { App.cursor = -1; },
};

/* One place that knows every shortcut, so the help sheet and the handler can
 * never drift apart. */
const MOD = /Mac|iPod|iPhone|iPad/.test(navigator.platform || "") ? "⌘" : "Ctrl+";

const SHORTCUTS = [
  ["1 – 5", "Jump to a section"],
  ["j / k", "Move down / up the list"],
  ["↵", "Open what the cursor is on"],
  ["esc", "Close the panel, or the sheet"],
  ["/", "Search the mail"],
  [`${MOD}K`, "Everything, from anywhere"],
  ["s", "Sort new mail"],
  ["c", "Check for new mail"],
  ["r", "Reload"],
  ["?", "This list"],
];

function onKey(event) {
  const typing = event.target.matches("input, select, textarea");

  if (event.key === "Escape") {
    if (App.overlay) { closeOverlay(); return event.preventDefault(); }
    if (typing) return event.target.blur();
    // Escape closes the detail panel before it does anything else: it is the
    // thing most likely to be open, and the thing you most want gone.
    const params = here();
    if (params.id) { delete params.id; go(App.route, params); return event.preventDefault(); }
    Keys.reset(); Keys.paint();
    return;
  }

  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    return openPalette();
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (App.overlay) return;   // the sheet owns the keyboard while it is up

  const routes = { 1: "today", 2: "cases", 3: "mail", 4: "review", 5: "setup" };
  if (routes[event.key]) { event.preventDefault(); return go(routes[event.key]); }

  switch (event.key) {
    case "j": case "ArrowDown":
      if (Keys.move(1)) event.preventDefault(); return;
    case "k": case "ArrowUp":
      if (Keys.move(-1)) event.preventDefault(); return;
    case "Enter":
      if (Keys.open()) event.preventDefault(); return;
    case "/":
      event.preventDefault();
      if (App.route !== "mail") return go("mail");
      return $('input[type="search"]')?.focus();
    case "?":
      event.preventDefault(); return openShortcuts();
    case "r": return refresh();
    case "s": return run("triage", {}, "Sorting new mail");
    case "c": return run("ingest", {}, "Checking for new mail");
    default:
  }
}

/* ------------------------------------------------------------- sheets --- */

function closeOverlay() {
  if (!App.overlay) return;
  const { node, restore } = App.overlay;
  App.overlay = null;
  node.remove();
  // Put the keyboard back where it was. Dropping focus on the body means the
  // next arrow key scrolls the window instead of moving the list cursor.
  if (restore && document.contains(restore)) restore.focus();
}

function openSheet(label, ...children) {
  const restore = document.activeElement;
  closeOverlay();
  const sheet = el("div", {
    class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": label,
  }, ...children);
  const scrim = el("div", {
    class: "scrim",
    onclick: (e) => { if (e.target === scrim) closeOverlay(); },
  }, sheet);
  document.body.append(scrim);
  App.overlay = { node: scrim, sheet, restore };
  return sheet;
}

/* A question with two answers, asked in the page's own voice rather than
 * the browser's: a native confirm() looks foreign here and may not be allowed
 * at all inside the desktop window. */
function confirmSheet(title, body, action) {
  return new Promise((resolve) => {
    const answer = (value) => { closeOverlay(); resolve(value); };
    const sheet = openSheet(title,
      el("div", { class: "sheet-body" },
        el("h2", { class: "sheet-title", text: title }),
        el("p", { class: "meta", text: body }),
        el("div", { class: "form-actions" },
          el("button", { class: "primary", onclick: () => answer(true) }, action),
          el("button", { onclick: () => answer(false) }, "Cancel"))));
    // Escape or a click outside closes the sheet: that is a "no".
    const observer = new MutationObserver(() => {
      if (!document.contains(sheet)) { observer.disconnect(); resolve(false); }
    });
    observer.observe(document.body, { childList: true });
  });
}

function openShortcuts() {
  openSheet("Keyboard shortcuts",
    el("dl", { class: "keys-grid" }, SHORTCUTS.map(([key, what]) =>
      [el("dt", {}, el("kbd", { text: key })), el("dd", { text: what })])),
    el("div", { class: "sheet-foot" }, el("span", { text: "esc to close" })));
}

/* --------------------------------------------------------- the palette --- */
/* One box that reaches everything: the sections, the jobs, and every case and
 * message by name. It is the difference between an app you learn and an app you
 * have to remember the layout of. */

/* Subsequence matching, scored so that a hit on a word boundary beats a hit in
 * the middle of a word — typing "na" should find "Nordwind Analytics" before it
 * finds "Internal". */
function fuzzy(needle, haystack) {
  if (!needle) return 0;
  const text = haystack.toLowerCase();
  let score = 0;
  let at = -1;
  for (const char of needle.toLowerCase()) {
    const found = text.indexOf(char, at + 1);
    if (found === -1) return -1;
    if (found === at + 1) score += 3;                              // run
    if (found === 0 || /[\s·—|-]/.test(text[found - 1])) score += 5;  // word start
    score += 1;
    at = found;
  }
  return score - text.length * 0.02;   // all else equal, the shorter name wins
}

async function openPalette() {
  const field = el("input", {
    class: "sheet-input", type: "text", placeholder: "Search, or jump to…",
    autocomplete: "off", spellcheck: "false", "aria-label": "Search everything",
  });
  const list = el("div", { class: "sheet-list", role: "listbox" });
  const sheet = openSheet("Search", el("div", { class: "sheet-head" }, icon("search", 17), field), list,
    el("div", { class: "sheet-foot" },
      el("span", {}, el("kbd", { text: "↑↓" }), " move"),
      el("span", {}, el("kbd", { text: "↵" }), " open"),
      el("span", {}, el("kbd", { text: "esc" }), " close")));

  const noun = App.state.entity_plural;
  const commands = [
    ["Go", "Today", "today", () => go("today")],
    ["Go", noun[0].toUpperCase() + noun.slice(1), "cases", () => go("cases")],
    ["Go", "Mail", "mail", () => go("mail")],
    ["Go", "Review", "review", () => go("review")],
    ["Go", "Setup", "setup", () => go("setup")],
    ["Do", "Check for new mail", "sync", () => run("ingest", {}, "Checking for new mail")],
    ["Do", "Sort new mail", "filter", () => run("triage", {}, "Sorting new mail")],
    ["Do", "Re-sort everything", "run", () => run("triage", { all: true }, "Sorting everything")],
    ["Do", "Show what has gone quiet", "quiet", () => go("cases", { quiet: "1" })],
    ["Do", "Keyboard shortcuts", "keys", () => openShortcuts()],
  ].map(([group, what, glyph, act]) => ({ group, what, glyph, where: "", act }));

  let entries = commands;
  let cursor = 0;

  // Cases and messages are loaded once the sheet is up, so it opens instantly
  // and fills in — waiting on two requests before showing anything would make
  // the fastest path in the app the slowest.
  const draw = () => {
    const needle = field.value.trim();
    const hits = entries
      .map((entry) => ({ entry, score: needle ? fuzzy(needle, `${entry.what} ${entry.where}`) : 0 }))
      .filter((hit) => hit.score >= 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, needle ? 12 : 10);
    cursor = Math.min(cursor, Math.max(0, hits.length - 1));

    if (!hits.length) {
      list.replaceChildren(el("div", { class: "sheet-empty", text: `Nothing matches “${needle}”` }));
      return;
    }
    const nodes = [];
    let group = null;
    hits.forEach((hit, index) => {
      if (!needle && hit.entry.group !== group) {
        group = hit.entry.group;
        nodes.push(el("div", { class: "sheet-group", text: group === "Go" ? "Go to" : "Run" }));
      }
      nodes.push(el("button", {
        class: "sheet-item", "data-cursor": index === cursor ? "1" : null,
        onmousemove: () => { if (cursor !== index) { cursor = index; draw(); } },
        onclick: () => { closeOverlay(); hit.entry.act(); },
      },
        icon(hit.entry.glyph || "arrow", 15),
        el("span", { class: "what", text: hit.entry.what }),
        hit.entry.where ? el("span", { class: "where", text: hit.entry.where }) : null));
    });
    list.replaceChildren(...nodes);
    list.querySelector('[data-cursor="1"]')?.scrollIntoView({ block: "nearest" });
  };

  field.addEventListener("input", () => { cursor = 0; draw(); });
  field.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    if (step) {
      event.preventDefault();
      const total = list.querySelectorAll(".sheet-item").length;
      if (total) cursor = (cursor + step + total) % total;
      return draw();
    }
    if (event.key === "Enter") {
      event.preventDefault();
      list.querySelectorAll(".sheet-item")[cursor]?.click();
    }
  });

  draw();
  field.focus();

  try {
    const [cases, mail] = await Promise.all([
      api("/cases").catch(() => ({ cases: [] })),
      api("/messages?limit=60").catch(() => ({ messages: [] })),
    ]);
    if (App.overlay?.sheet !== sheet) return;   // closed while loading
    entries = [
      ...commands,
      ...(cases.cases || []).map((c) => ({
        group: "Case", glyph: "cases",
        what: [c.org || c.title, c.role].filter(Boolean).join(" — "),
        where: humanise(c.state),
        act: () => go("cases", { id: c.id }),
      })),
      ...(mail.messages || []).map((m) => ({
        group: "Mail", glyph: "mail",
        what: m.subject || "(no subject)",
        where: m.from_name || m.from_addr || "",
        act: () => go("mail", { id: m.message_id }),
      })),
    ];
    draw();
  } catch { /* the commands alone are still a working palette */ }
}

/* -------------------------------------------------------------- routing --- */

const here = () => Object.fromEntries(new URLSearchParams(location.hash.split("?")[1] || ""));

function go(route, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined));
  location.hash = `#/${route}${query.toString() ? `?${query}` : ""}`;
}

const ROUTES = {
  today: viewToday, cases: viewCases, mail: viewMail, review: viewReview, setup: viewSetup,
  welcome: viewWelcome,
};

async function render() {
  closeDrawer();
  const [path, rawQuery] = location.hash.replace(/^#\/?/, "").split("?");
  let route = ROUTES[path] ? path : "today";
  // A new install has nothing to show on any screen yet; the welcome is the
  // only useful place to be until something has been read or it is skipped.
  if (App.state && App.state.setup && App.state.setup.first_run && route !== "welcome") {
    route = "welcome";
    history.replaceState(null, "", "#/welcome");
  }
  const moved = route !== App.route;
  App.route = route;
  App.arriving = moved;
  if (route !== "review") App.reviewIndex = 0;
  if (moved) Keys.reset();   // a new screen starts with no cursor on it
  paintNav();
  paintCrumbs(route);

  // 120ms is about where a blank frame stops reading as "instant" and starts
  // reading as "stalled". Under that, showing a placeholder would flash.
  const placeholder = setTimeout(() => paintSkeleton(route), 120);
  try {
    await ROUTES[route](new URLSearchParams(rawQuery || ""));
  } catch (err) {
    toast(err.message, true);
    paint(el("div", { class: "panel panel-pad" }, empty("That did not load", err.message,
      el("button", { onclick: () => render() }, "Try again"), "alert")));
  } finally {
    clearTimeout(placeholder);
  }
}

function paintCrumbs(route) {
  const label = {
    today: "Today", cases: capital(App.state.entity_plural), mail: "Mail",
    review: "Review", setup: "Setup", welcome: "Welcome",
  }[route] || "";
  $("#crumbs").replaceChildren(icon(route === "welcome" ? "sparkle" : route, 15),
    el("span", { text: "Bmail" }), el("span", { class: "sep", text: "/" }), el("b", { text: label }));
}

function paintNav() {
  const s = App.state;
  const review = s.verdicts.needs_review || 0;
  // A badge should mean "something is waiting for you", never "here is a
  // volume". A count of all mail tells nobody anything.
  const groups = [
    ["Your mail", [
      ["today", "Today", s.needs_you ?? (s.verdicts.action_required || 0), "urgent"],
      ["cases", s.entity_plural[0].toUpperCase() + s.entity_plural.slice(1),
        s.cases.stalled || 0, "quiet"],
      ["mail", "Mail", 0, null],
      ["review", "Review", review, "urgent"],
    ]],
    ["Settings", [
      ["setup", "Setup", 0, null],
    ]],
  ];
  const tab = ([route, label, count, flag]) =>
    el("a", {
      href: `#/${route}`, "data-tab": route, "data-flag": count ? flag : null,
      "aria-current": App.route === route ? "page" : null,
    },
      icon(route),
      el("span", { text: label }),
      count ? el("span", { class: "count", text: String(count) }) : null);

  // The pill is the same element across every redraw, re-inserted rather than
  // rebuilt: that is what lets it travel from the old item to the new one
  // instead of appearing in place.
  const pill = NavPill.node();
  $("#nav").replaceChildren(pill, ...groups.flatMap(([name, items], index) => [
    index ? el("div", { class: "nav-rule" }) : null,
    el("div", { class: "nav-group", text: name }),
    ...items.map(tab),
  ].filter(Boolean)));
  NavPill.follow();
}

const NavPill = {
  element: null,
  placed: false,
  node() {
    if (!this.element) this.element = el("div", { class: "nav-pill settle", "aria-hidden": "true" });
    return this.element;
  },
  /** Slide to the current item. The first placement, and any placement after a
   *  resize, jumps: travelling from nowhere, or because the window changed
   *  shape, would be motion that reports nothing. */
  follow(jump = false) {
    const pill = this.node();
    const current = $('#nav a[aria-current="page"]');
    if (!current) { pill.style.opacity = "0"; return; }
    pill.classList.toggle("settle", jump || !this.placed);
    pill.style.opacity = "1";
    pill.style.width = `${current.offsetWidth}px`;
    pill.style.height = `${current.offsetHeight}px`;
    pill.style.transform = `translate(${current.offsetLeft}px, ${current.offsetTop}px)`;
    if (!this.placed) {
      this.placed = true;
      requestAnimationFrame(() => pill.classList.remove("settle"));
    }
  },
};

/* --------------------------------------------------------------- boot --- */

async function refresh() {
  await loadState();
  await render();
}

/* The state and everything drawn from it outside the page: the counts in the
   sidebar and its footer. Never the page itself. */
async function loadState() {
  App.packChoice = localStorage.getItem("bmail.pack") || null;
  App.mailKey = null;   // the data changed: the next mail screen fetches its list afresh
  const state = await api("/state");
  // The product should speak the domain's language, not the engine's.
  state.entity = humanise(state.entity || "case");
  state.entity_plural = state.entity.endsWith("s") ? state.entity : `${state.entity}s`;
  App.state = state;

  $("#rail-pack").textContent = state.pack;
  $("#rail-meta").replaceChildren(
    el("div", { text: `${state.messages.total} message${state.messages.total === 1 ? "" : "s"} read` }),
    el("div", { text: `${state.cases.total} ${state.entity_plural} tracked` }),
    el("div", {}, el("span", { class: "dot", text: "●" }), " ",
      state.llm.available && state.llm.pulled ? state.llm.model : "rules + classifier"));
  const owner = state.owner || {};
  const who = owner.name || (owner.address ? owner.address.split("@")[0] : "You");
  $("#account-name").textContent = who;
  $("#account-address").textContent = owner.address || "No mailbox connected";
  const face = $("#account-avatar");
  face.textContent = initials(owner.name, owner.address).toUpperCase();
  face.dataset.hue = hueOf(owner.address || who);
  if (App.route) paintNav();
}

async function boot() {
  document.querySelectorAll(".mark").forEach((node) => node.replaceChildren(mark()));
  const url = new URL(location.href);
  const supplied = url.searchParams.get("token");
  if (supplied) {
    sessionStorage.setItem("bmail.token", supplied);
    // Keep the token out of the address bar, history and any screenshot.
    url.searchParams.delete("token");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  App.token = sessionStorage.getItem("bmail.token");

  const fail = (message) => $("#boot-msg").replaceChildren(
    document.createTextNode(message), el("code", { text: "bmail ui" }));

  if (!App.token) return fail("This page needs a session key. Start Bmail with:");
  try { await Sync.poll(); await refresh(); }
  catch (err) { return fail(`The engine did not answer (${err.message}). Start it with:`); }

  $("#boot").hidden = true;
  $("#shell").hidden = false;
  // The first placement ran while the shell was still hidden, when every item
  // measures zero. Place it again now there is a layout to measure.
  NavPill.follow(true);

  window.addEventListener("hashchange", render);
  $("#btn-sync").onclick = () => run("ingest", {}, "Checking for new mail");
  $("#btn-find").onclick = () => openPalette();
  $("#btn-keys").onclick = () => openShortcuts();
  $("#find-key").textContent = `${MOD}K`;
  $("#btn-find").prepend(icon("search", 15));
  $("#btn-sync").prepend(icon("sync", 15));
  $("#btn-keys").prepend(icon("keys", 14));
  // A resize can change where every item sits; the pill jumps to match rather
  // than sliding, since nothing about the selection moved.
  let resizing;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(resizing);
    resizing = requestAnimationFrame(() => NavPill.follow(true));
  });
  document.addEventListener("keydown", onKey);
  // A hidden tab stops asking; coming back asks straight away.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) Sync.poll(); });

  const jobs = await api("/jobs").catch(() => ({ jobs: [] }));
  const running = (jobs.jobs || []).find((j) => ["queued", "running"].includes(j.status));
  if (running) { paintJob(running); watchJob(running.id); }
}

if (!location.hash) location.hash = "#/today";
boot();
