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

/* Icons are drawn here rather than pulled from a set, because a local-first
 * app cannot fetch one and a font of glyphs picked for their shape ends up
 * looking like five different sets. One grid, one stroke width. */
const SVG_NS = "http://www.w3.org/2000/svg";

const ICONS = {
  today:  ["M4 6.5h9", "M4 11h13", "M4 15.5h6"],
  cases:  ["M10 2.5 2.5 6.5 10 10.5l7.5-4z", "M2.5 10.5 10 14.5l7.5-4"],
  mail:   ["M2.5 4.5h15v11h-15z", "m2.8 5 7.2 5 7.2-5"],
  review: ["M10 2.5a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15z",
           "M8 8a2 2 0 1 1 2.6 1.9v1.3", "M10.6 14.2h-.01"],
  setup:  ["M3 5.5h5", "M11 5.5h6", "M3 10h9", "M15 10h2", "M3 14.5h4", "M10 14.5h7"],
  search: ["M9 15.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13z", "m13.8 13.8 3.7 3.7"],
};

function icon(name, size = 14) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name] || []) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

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

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
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
  toast.timer = setTimeout(() => {
    node.classList.add("leaving");
    toast.gone = setTimeout(() => { node.hidden = true; }, 220);
  }, isError ? 8000 : 3600);
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
  if (!job || ["done", "error", "cancelled"].includes(job.status)) { bar.hidden = true; return; }
  bar.hidden = false;
  $("#job-label").textContent = label || job.kind;
  $("#job-fill").style.width = `${Math.round((job.progress || 0) * 100)}%`;
  $("#job-cancel").onclick = () => post(`/jobs/${job.id}/cancel`).catch(() => {});
}

function watchJob(jobId, label) {
  clearInterval(App.poll);
  App.poll = setInterval(async () => {
    let job;
    try { job = await api(`/jobs/${jobId}`); }
    catch { clearInterval(App.poll); return; }
    paintJob(job, label);
    if (!["done", "error", "cancelled"].includes(job.status)) return;
    clearInterval(App.poll);
    if (job.status === "error") toast(job.error, true);
    else if (job.status === "cancelled") toast("Stopped.");
    else toast(jobWords(job));
    await refresh();
  }, 500);
}

function jobWords(job) {
  const result = job.result || {};
  if (job.kind === "triage") {
    return result.processed ? `Sorted ${result.processed} messages.` : "Nothing new to sort.";
  }
  if (job.kind === "ingest") {
    const total = Object.values(result.fetched || {}).reduce((a, b) => a + b, 0);
    return total ? `${total} new messages.` : "No new mail.";
  }
  if (job.kind === "export") return `Written to ${result.directory}`;
  return "Done.";
}

/* --------------------------------------------------------------- views --- */

function paint(...nodes) {
  const main = $("#main");
  const wide = App.route === "mail" ? " wide" : "";
  // Animate only when a new screen arrives. Selecting a row, running a search
  // or changing a filter redraws the same screen, and replaying the entrance
  // for those makes the whole list flinch every time you click something —
  // motion reporting a change that did not happen.
  const arriving = App.arriving && !App.skeleton;
  main.replaceChildren(el("div", { class: `page${wide}${arriving ? "" : " instant"}` }, ...nodes));
  // Scrolling back to the top belongs to arriving somewhere new. Doing it on a
  // redraw throws away the place you had scrolled to in order to click.
  if (arriving) main.scrollTop = 0;
  App.skeleton = false;
  App.arriving = false;
  Keys.rescan();
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

const empty = (title, body, action) =>
  el("div", { class: "empty" },
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

  // The window picker belongs with the date it qualifies, not stranded at the
  // bottom of the page — it changes everything above it.
  const header = el("div", { class: "bar-row flush" },
    el("span", { class: "eyebrow", text: today }),
    el("span", { class: "grow" }),
    state.verdicts.total
      ? el("select", { onchange: (e) => {
          localStorage.setItem("bmail.window", e.target.value); render();
        } },
        [[1, "Today"], [7, "Last 7 days"], [30, "Last 30 days"], [3650, "Everything"]]
          .map(([value, label]) => el("option", { value, selected: value === days }, label)))
      : null);

  const nodes = [header, ...headlineFor(counts, days, state)];

  if (brief.needs_you.length) {
    const more = (counts.needs_you || 0) - brief.needs_you.length;
    nodes.push(section("Needs you", counts.needs_you));
    nodes.push(el("div", { class: "stack" }, brief.needs_you.map(briefCard)));
    if (more > 0) {
      nodes.push(el("div", { class: "bar-row" },
        el("button", { class: "sm", onclick: () => go("mail", { action: "1" }) },
          `Show the other ${more}`)));
    }
  }

  if (brief.deadlines.length) {
    nodes.push(section("Deadlines", brief.deadlines.length));
    nodes.push(el("div", { class: "stack" }, brief.deadlines.map((item) => {
      const tone = toneFor({ due: item.due });
      return el("a", { class: "card", "data-tone": tone, href: `#/cases?id=${item.case_id || ""}` },
        el("div", { class: "card-top" },
          el("span", { class: "flag when", "data-tone": tone, text: overdueWords(item.due) })),
        el("div", { class: "card-title", text: item.title }),
        el("div", { class: "card-org", text: dateWords(item.due) }));
    })));
  }

  if (brief.events.length) {
    nodes.push(section("Diary", brief.events.length));
    nodes.push(el("div", { class: "stack" }, brief.events.map((item) =>
      el("a", { class: "card", "data-tone": "live", href: `#/cases?id=${item.case_id || ""}` },
        el("div", { class: "card-top" },
          el("span", { class: "flag when", "data-tone": "live", text: whenWords(item.when) })),
        el("div", { class: "card-title", text: item.title }),
        el("div", { class: "card-org", text: dateWords(item.when, true) })))));
  }

  if (brief.stalled.length) {
    const worth = brief.stalled.slice(0, 8);
    nodes.push(section("No reply", brief.stalled.length));
    nodes.push(el("div", { class: "rows", "data-list": "quiet" },
      worth.map((item) => el("a", { class: "row", href: `#/cases?id=${item.case_id || ""}` },
        el("span", { class: "avatar", text: initials(item.title) }),
        el("span", { class: "row-main" },
          el("span", { class: "row-title", text: item.title }),
          el("span", { class: "row-sub", text: humanise(item.detail.split("·").pop()) })),
        // The silence, as a column of numbers you can run your eye down.
        el("span", { class: "row-side" },
          el("span", { class: "meta", text: `${Math.round(item.score)}d silent` }))))));
    if (brief.stalled.length > worth.length) {
      nodes.push(el("div", { class: "bar-row" },
        el("button", { class: "sm", onclick: () => go("cases", { quiet: "1" }) },
          `Show all ${brief.stalled.length}`)));
    }
  }

  if (counts.archived) {
    nodes.push(section("Filed", counts.archived));
    nodes.push(el("div", { class: "bar-row" },
      el("span", { class: "meta", text: "Newsletters, alerts and automatic acknowledgements." }),
      el("span", { class: "grow" }),
      el("button", { class: "sm", onclick: () => go("mail") }, "Open")));
  }

  paint(...nodes);
}

const section = (label, count) =>
  el("h2", { class: "section" },
    el("span", { text: label }),
    count != null ? el("span", { class: "n", text: String(count) }) : null);

function headlineFor(counts, days, state) {
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
  if (needs) {
    const extra = [];
    if (counts.deadlines) extra.push(`${counts.deadlines} with a deadline`);
    if (counts.events) extra.push(`${counts.events} in the diary`);
    return [
      el("h1", { class: "headline" },
        el("span", { class: "n", text: String(needs) }),
        needs === 1 ? " needs you" : " need you"),
      extra.length ? el("p", { class: "subhead", text: extra.join(" · ") }) : null,
    ];
  }
  const outstanding = state.verdicts.action_required || 0;
  if (outstanding && days < 3650) {
    return [
      el("h1", { class: "headline", text: "Clear for this window" }),
      el("p", { class: "subhead", text: `${outstanding} open across everything Bmail has read.` }),
      el("div", { class: "bar-row" },
        el("button", { class: "sm", onclick: () => {
          localStorage.setItem("bmail.window", "3650"); render();
        } }, "Show everything")),
    ];
  }
  return [
    el("h1", { class: "headline", text: "All clear" }),
    el("p", { class: "subhead", text: "Nothing is waiting on you." }),
  ];
}

function briefCard(item) {
  const tone = toneFor({ due: item.due, urgency: item.urgency, actionRequired: true });
  const org = [item.org, item.role].filter(Boolean).join(" · ") || item.detail;
  const category = humanise(item.category);
  const timing = item.due ? overdueWords(item.due) : item.when ? whenWords(item.when) : "";
  return el("a", {
    class: "card", "data-tone": tone,
    href: item.message_id ? `#/mail?id=${encodeURIComponent(item.message_id)}` : `#/cases?id=${item.case_id || ""}`,
  },
    el("div", { class: "card-top" },
      category ? el("span", { class: "chip", "data-tone": timing ? null : tone, text: category }) : null,
      timing ? el("span", { class: "flag when", "data-tone": tone, text: timing }) : null),
    el("div", { class: "card-title", text: item.title }),
    el("div", { class: "card-org", text: org }),
    item.evidence ? el("blockquote", { class: "quote", text: item.evidence }) : null);
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
    el("button", { class: onlyQuiet ? "on" : "", onclick: () => go("cases", onlyQuiet ? {} : { quiet: "1" }) },
      "No reply"),
    el("span", { class: "grow" }),
    el("span", { class: "meta", text: `${data.cases.length} ${noun}` }));

  if (!data.cases.length) {
    paint(el("h1", { class: "headline", text: noun[0].toUpperCase() + noun.slice(1) }), controls,
      el("div", { class: "panel panel-pad" }, empty("Nothing tracked yet",
        `A ${App.state.entity} opens the first time Bmail sees mail about one.`,
        el("button", { class: "primary", onclick: () => go("setup") }, "Connect a mailbox"))));
    return;
  }

  // Live before closed: a rejection from July is history, not news.
  const ordered = [...data.cases].sort((a, b) =>
    (a.closed ? 1 : 0) - (b.closed ? 1 : 0)
    || (b.stalled ? 1 : 0) - (a.stalled ? 1 : 0)
    || String(b.last_event_at || "").localeCompare(String(a.last_event_at || "")));
  const open = ordered.filter((c) => !c.closed);
  const closed = ordered.filter((c) => c.closed);

  const list = el("div", {},
    el("div", { class: "stack" }, open.map((c) => caseCard(c, data.states, selected))),
    closed.length
      ? el("div", {},
          section("Closed", closed.length),
          el("div", { class: "stack closed" },
            closed.map((c) => caseCard(c, data.states, selected))))
      : null);
  paint(
    el("h1", { class: "headline", text: noun[0].toUpperCase() + noun.slice(1) }),
    controls,
    selected
      ? el("div", { class: "split" }, list, el("div", { class: "aside" }, await caseAside(selected)))
      : list);
}

function caseCard(c, states, selected) {
  const silent = c.last_inbound_at ? -daysFrom(c.last_inbound_at) : null;
  const tone = toneFor({ stalled: c.stalled, closed: c.closed, due: c.next_action_due, state: c.state });
  return el("a", {
    class: "card", "data-tone": tone, href: `#/cases?id=${c.id}`,
    "aria-selected": String(c.id) === String(selected),
  },
    // Right-hand column: only what is true of this case right now. The stage
    // it has reached is drawn underneath, so it is not repeated as a word here.
    el("div", { class: "card-top" },
      c.stalled && silent
        ? el("span", { class: "chip", "data-tone": "quiet", text: `${silent}d silent` }) : null,
      !c.closed && c.next_action_due
        ? el("span", { class: "chip", "data-tone": toneFor({ due: c.next_action_due }),
            text: overdueWords(c.next_action_due) }) : null,
      !c.closed && c.event_datetime
        ? el("span", {
            class: "chip",
            // An interview that has already happened is history, not a plan.
            "data-tone": (daysFrom(c.event_datetime) ?? -1) >= 0 ? "live" : null,
            text: dateWords(c.event_datetime, true),
          }) : null,
      el("span", { class: "meta when",
        text: c.last_event_at ? agoWords(c.last_event_at) : `${c.message_count} messages` })),
    el("div", { class: "card-title", text: c.org || c.title || "Untitled" }),
    el("div", { class: "card-org", text: c.role || `${c.message_count} messages` }),
    stageRail(c, states));
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
  catch (err) { return el("div", { class: "panel panel-pad" }, empty("Not found", err.message)); }
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

  const query = new URLSearchParams({ limit: "60", offset: String(offset) });
  if (q) query.set("q", q);
  if (category) query.set("category", category);
  if (onlyAction) query.set("action", "1");
  const data = await api(`/messages?${query}`);
  const selected = params.get("id");

  const search = el("input", {
    type: "search", placeholder: "Search", value: q,
    onkeydown: (e) => { if (e.key === "Enter") go("mail", { ...here(), q: e.target.value, offset: 0 }); },
  });

  const controls = el("div", { class: "bar-row" },
    search,
    el("select", { onchange: (e) => go("mail", { ...here(), category: e.target.value, offset: 0 }) },
      el("option", { value: "" }, "All kinds"),
      (App.state.categories || []).map((c) =>
        el("option", { value: c.name, selected: c.name === category }, humanise(c.name)))),
    el("button", { class: onlyAction ? "on" : "",
      onclick: () => go("mail", { ...here(), action: onlyAction ? "" : "1", offset: 0 }) }, "Needs me"),
    el("span", { class: "grow" }));

  if (!data.messages.length) {
    paint(el("h1", { class: "headline", text: "Mail" }), controls,
      el("div", { class: "panel panel-pad" }, empty(q ? "No match" : "No mail yet",
        q ? `Nothing matches “${q}”.` : "Connect a mailbox and Bmail will read it.",
        q ? null : el("button", { class: "primary", onclick: () => go("setup") }, "Connect a mailbox"))));
    return;
  }

  const rows = el("div", { class: "rows", "data-list": "mail" },
    data.messages.map((m) => mailRow(m, selected)));
  const pager = el("div", { class: "bar-row" },
    el("span", { class: "meta", text: `${offset + 1}–${offset + data.messages.length} of ${data.total}` }),
    el("span", { class: "grow" }),
    el("button", { class: "sm", disabled: offset === 0,
      onclick: () => go("mail", { ...here(), offset: Math.max(0, offset - 60) }) }, "Newer"),
    el("button", { class: "sm", disabled: offset + 60 >= data.total,
      onclick: () => go("mail", { ...here(), offset: offset + 60 }) }, "Older"));

  const left = el("div", {}, rows, pager);
  paint(el("h1", { class: "headline", text: "Mail" }), controls,
    selected
      ? el("div", { class: "split" }, left, el("div", { class: "aside" }, await mailAside(selected)))
      : left);
}

function mailRow(m, selected) {
  const tone = toneFor({ due: m.deadline, urgency: m.urgency, actionRequired: m.action_required });
  return el("a", {
    class: "row", href: `#/mail?${new URLSearchParams({ ...here(), id: m.message_id })}`,
    "aria-selected": m.message_id === selected,
  },
    el("span", { class: "avatar", text: initials(m.from_name, m.from_addr) }),
    el("span", { class: "row-main" },
      el("span", { class: "row-title", text: m.subject || "(no subject)" }),
      el("span", { class: "row-sub", text: m.summary || m.from_name || m.from_addr })),
    // Date last: a ragged right edge of chips is unreadable, a column of dates
    // is something you can run your eye down.
    el("span", { class: "row-side" },
      m.category ? el("span", { class: "chip", "data-tone": tone, text: humanise(m.category) }) : null,
      el("span", { class: "meta when", text: agoWords(m.date_utc) })));
}

async function mailAside(messageId) {
  let data;
  try { data = await api(`/messages/${encodeURIComponent(messageId)}`); }
  catch (err) { return el("div", { class: "panel panel-pad" }, empty("Not found", err.message)); }
  const { message: m, verdict: v, case: kase } = data;

  return el("div", { class: "panel panel-pad" },
    el("div", { class: "eyebrow", text: dateWords(m.date, true) }),
    el("div", { class: "panel-title", text: m.subject || "(no subject)" }),
    el("div", { class: "card-org", text: `${m.from.name || ""} ${m.from.address}`.trim() }),

    v && v.evidence
      ? el("div", {},
          el("h3", { text: v.evidence_ok === false ? "Evidence · not in this email" : "Evidence" }),
          el("blockquote", { class: `quote${v.evidence_ok === false ? " bad" : ""}`, text: v.evidence }))
      : null,

    el("h3", { text: "Verdict" }),
    el("dl", { class: "facts" },
      v ? fact("Kind", humanise(v.category)) : null,
      v && v.action_required ? fact("Action", humanise(v.next_action)) : null,
      v && v.deadline ? fact("Deadline", `${v.deadline} · ${whenWords(v.deadline)}`) : null,
      v && v.event_datetime ? fact("Diary", dateWords(v.event_datetime, true)) : null,
      v ? fact("Decided by", `${humanise(v.stage)} · ${Math.round((v.confidence || 0) * 100)}%`) : null,
      kase ? fact("Part of", kase.title) : null,
      m.attachments.length ? fact("Attached", m.attachments.map((a) => a.filename).join(", ")) : null),

    v ? el("div", {},
      el("h3", { text: "Correct it" }),
      correctionControls(messageId, v)) : null,

    el("h3", { text: "Message" }),
    el("div", { class: "body-text", text: m.body_text || "(no text)" }));
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

/* --- Setup -------------------------------------------------------------- */

function viewSetup() {
  const s = App.state;
  const path = el("input", { type: "text", class: "grow-field", placeholder: "~/mail/archive.mbox" });
  const account = el("select", {},
    el("option", { value: "" }, "— pick an account —"),
    (s.configured_accounts || []).map((a) =>
      el("option", { value: a.name }, `${a.name} · ${a.provider}`)));
  const packs = el("select", { onchange: (e) => {
    localStorage.setItem("bmail.pack", e.target.value);
    // Filters name states of the pack that is leaving; drop them with it.
    location.hash = "#/today";
    location.reload();
  } }, (s.packs || []).map((p) => el("option", { value: p, selected: p === s.pack }, p)));
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

    el("h2", { class: "section", text: "Mail" }),
    group("Configured account", account,
      el("button", { onclick: () => run("ingest", { account: account.value || null }, "Collecting mail") },
        "Collect")),
    group("File on this machine", path,
      el("button", { onclick: () => path.value.trim()
        ? run("ingest", { path: path.value.trim(), account: "local" }, "Reading file")
        : toast("Give me a path first", true) }, "Read")),
    note("An mbox, a Maildir or a folder of .eml files. A Gmail Takeout export works."),

    el("h2", { class: "section", text: "Sorting" }),
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

    el("h2", { class: "section", text: "Appearance" }),
    group("Layout",
      el("select", { onchange: (e) => { applyTheme(e.target.value); render(); } },
        Object.entries(THEMES).map(([value, label]) =>
          el("option", { value, selected: value === document.documentElement.dataset.theme },
            label)))),
    note("Studio is roomier and easier to read. Compact fits about half again as "
       + "much on a screen, which starts to matter at ninety applications."),

    el("h2", { class: "section", text: "Domain" }),
    group("Pack", packs,
      el("span", { class: "meta",
        text: `${s.entity_plural} · ${(s.states || []).length} stages` })),

    el("h2", { class: "section", text: "Export" }),
    group("Formats", formats,
      el("button", { onclick: () => {
        const kinds = formats.map((f) => f.querySelector("input")).filter((i) => i.checked)
          .map((i) => i.value);
        if (!kinds.length) return toast("Pick a format first", true);
        run("export", { kinds }, "Writing files");
      } }, "Write files")),
    note("A spreadsheet, or a calendar of every deadline and interview."),

    el("h2", { class: "section", text: "Storage" }),
    el("dl", { class: "facts set-facts" },
      fact("Database", s.database),
      fact("Folder", s.home),
      fact("Messages", `${s.messages.total}`),
      fact("Sorted", `${s.verdicts.total}`),
      fact("Tracked", `${s.cases.total} ${s.entity_plural}`)),

    el("h2", { class: "section", text: "Keys" }),
    el("dl", { class: "facts set-facts" },
      [["1", "Today"], ["2", "Cases"], ["3", "Mail"], ["4", "Review"], ["5", "Setup"],
       ["r", "Reload"], ["s", "Sort new mail"]].map(([key, what]) =>
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
    this.items[App.cursor].scrollIntoView({ block: "nearest" });
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
  const sheet = openSheet("Search", field, list,
    el("div", { class: "sheet-foot" },
      el("span", {}, el("kbd", { text: "↑↓" }), " move"),
      el("span", {}, el("kbd", { text: "↵" }), " open"),
      el("span", {}, el("kbd", { text: "esc" }), " close")));

  const noun = App.state.entity_plural;
  const commands = [
    ["Go", "Today", () => go("today")],
    ["Go", noun[0].toUpperCase() + noun.slice(1), () => go("cases")],
    ["Go", "Mail", () => go("mail")],
    ["Go", "Review", () => go("review")],
    ["Go", "Setup", () => go("setup")],
    ["Do", "Check for new mail", () => run("ingest", {}, "Checking for new mail")],
    ["Do", "Sort new mail", () => run("triage", {}, "Sorting new mail")],
    ["Do", "Re-sort everything", () => run("triage", { all: true }, "Sorting everything")],
    ["Do", "Show what has gone quiet", () => go("cases", { quiet: "1" })],
    ["Do", "Keyboard shortcuts", () => openShortcuts()],
  ].map(([group, what, act]) => ({ group, what, where: "", act }));

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
        group: "Case",
        what: [c.org || c.title, c.role].filter(Boolean).join(" — "),
        where: humanise(c.state),
        act: () => go("cases", { id: c.id }),
      })),
      ...(mail.messages || []).map((m) => ({
        group: "Mail",
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

const ROUTES = { today: viewToday, cases: viewCases, mail: viewMail, review: viewReview, setup: viewSetup };

async function render() {
  const [path, rawQuery] = location.hash.replace(/^#\/?/, "").split("?");
  const route = ROUTES[path] ? path : "today";
  const moved = route !== App.route;
  App.route = route;
  App.arriving = moved;
  if (route !== "review") App.reviewIndex = 0;
  if (moved) Keys.reset();   // a new screen starts with no cursor on it
  paintNav();

  // 120ms is about where a blank frame stops reading as "instant" and starts
  // reading as "stalled". Under that, showing a placeholder would flash.
  const placeholder = setTimeout(() => paintSkeleton(route), 120);
  try {
    await ROUTES[route](new URLSearchParams(rawQuery || ""));
  } catch (err) {
    toast(err.message, true);
    paint(el("div", { class: "panel panel-pad" }, empty("That did not load", err.message,
      el("button", { onclick: () => render() }, "Try again"))));
  } finally {
    clearTimeout(placeholder);
  }
}

function paintNav() {
  const s = App.state;
  const review = s.verdicts.needs_review || 0;
  // A badge should mean "something is waiting for you", never "here is a
  // volume". A count of all mail tells nobody anything.
  const groups = [
    ["Your mail", [
      ["today", "Today", s.verdicts.action_required || 0, "urgent"],
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

  $("#nav").replaceChildren(...groups.flatMap(([name, items], index) => [
    index ? el("div", { class: "nav-rule" }) : null,
    el("div", { class: "nav-group", text: name }),
    ...items.map(tab),
  ].filter(Boolean)));
}

/* --------------------------------------------------------------- boot --- */

async function refresh() {
  App.packChoice = localStorage.getItem("bmail.pack") || null;
  const state = await api("/state");
  // The product should speak the domain's language, not the engine's.
  state.entity = humanise(state.entity || "case");
  state.entity_plural = state.entity.endsWith("s") ? state.entity : `${state.entity}s`;
  App.state = state;

  $("#rail-pack").textContent = state.pack;
  $("#rail-meta").replaceChildren(
    el("div", { text: `${state.messages.total} messages read` }),
    el("div", { text: `${state.cases.total} ${state.entity_plural} tracked` }),
    el("div", {}, el("span", { class: "dot", text: "●" }), " ",
      state.llm.available && state.llm.pulled ? state.llm.model : "rules + classifier"));
  await render();
}

async function boot() {
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
  try { await refresh(); }
  catch (err) { return fail(`The engine did not answer (${err.message}). Start it with:`); }

  $("#boot").hidden = true;
  $("#shell").hidden = false;

  window.addEventListener("hashchange", render);
  $("#btn-sync").onclick = () => run("ingest", {}, "Checking for new mail");
  $("#btn-find").onclick = () => openPalette();
  $("#btn-keys").onclick = () => openShortcuts();
  $("#find-key").textContent = `${MOD}K`;
  $("#btn-find").prepend(icon("search", 15));
  document.addEventListener("keydown", onKey);

  const jobs = await api("/jobs").catch(() => ({ jobs: [] }));
  const running = (jobs.jobs || []).find((j) => ["queued", "running"].includes(j.status));
  if (running) { paintJob(running); watchJob(running.id); }
}

if (!location.hash) location.hash = "#/today";
boot();
