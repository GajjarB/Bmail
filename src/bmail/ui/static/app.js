/* Bmail — the desktop application.
 *
 * Framework-free on purpose: the page is served by the local daemon and must
 * work with the machine offline, so there is no CDN, no bundler and no build
 * step. The file you read is the file that runs.
 *
 * Two rules run through the whole file:
 *   - the product speaks in sentences, not statistics;
 *   - nothing is ever inserted as markup, so mail content cannot become HTML.
 */

"use strict";

const App = {
  token: null,
  state: null,
  route: "today",
  poll: null,
  reviewIndex: 0,
  packChoice: null,   // the pack the user picked, or null for the configured one
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

const NUMBERS = ["Nothing", "One thing", "Two things", "Three things", "Four things",
  "Five things", "Six things", "Seven things", "Eight things", "Nine things", "Ten things"];
const countWords = (n) => (n < NUMBERS.length ? NUMBERS[n] : `${n} things`);

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
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, isError ? 8000 : 3600);
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
    return result.processed ? `Read through ${result.processed} messages.` : "Nothing new to sort.";
  }
  if (job.kind === "ingest") {
    const total = Object.values(result.fetched || {}).reduce((a, b) => a + b, 0);
    return total ? `Collected ${total} new messages.` : "No new mail.";
  }
  if (job.kind === "export") return `Saved to ${result.directory}`;
  return "Done.";
}

/* --------------------------------------------------------------- views --- */

function paint(...nodes) {
  const main = $("#main");
  main.replaceChildren(el("div", { class: `page${App.route === "mail" ? " wide" : ""}` }, ...nodes));
  main.scrollTop = 0;
}

const empty = (mark, title, body, action) =>
  el("div", { class: "empty" },
    el("div", { class: "mark", text: mark }),
    el("h3", { text: title }),
    el("p", { text: body }),
    action);

/* --- Today ---------------------------------------------------------- */

async function viewToday() {
  const days = Number(localStorage.getItem("bmail.window") || 7);
  const brief = await api(`/brief?days=${days}&limit=12`);
  const counts = brief.counts || {};
  const state = App.state;

  const today = new Date().toLocaleDateString(undefined,
    { weekday: "long", day: "numeric", month: "long" });

  const nodes = [
    el("div", { class: "eyebrow", text: today }),
    ...headlineFor(counts, days, state),
  ];

  if (brief.needs_you.length) {
    const more = (counts.needs_you || 0) - brief.needs_you.length;
    nodes.push(el("h2", { class: "section",
      text: more > 0 ? `Needs you · top ${brief.needs_you.length} of ${counts.needs_you}` : "Needs you" }));
    nodes.push(el("div", { class: "stack" }, brief.needs_you.map(briefCard)));
    if (more > 0) {
      nodes.push(el("div", { class: "bar-row" },
        el("button", { class: "sm", onclick: () => go("mail", { action: "1" }) },
          `See the other ${more}`)));
    }
  }

  if (brief.events.length) {
    nodes.push(el("h2", { class: "section", text: "Coming up" }));
    nodes.push(el("div", { class: "stack" }, brief.events.map((item) =>
      el("a", { class: "card", "data-tone": "live", href: `#/cases?id=${item.case_id || ""}` },
        el("div", { class: "flag", "data-tone": "live", text: whenWords(item.when) }),
        el("div", { class: "card-title", text: item.title }),
        el("div", { class: "card-org", text: dateWords(item.when, true) })))));
  }

  if (brief.stalled.length) {
    const worth = brief.stalled.slice(0, 6);
    nodes.push(el("h2", { class: "section", text: "Gone quiet" }));
    nodes.push(el("div", { class: "card", "data-tone": "quiet" },
      el("div", { class: "card-title",
        text: `${brief.stalled.length} ${brief.stalled.length === 1 ? "case has" : "cases have"} had no reply for a while.` }),
      el("div", { class: "card-org",
        text: "No spreadsheet tells you this. These are the ones worth a nudge." }),
      el("div", { class: "stack", "data-list": "quiet", style: "margin-top:14px;gap:6px" },
        worth.map((item) => el("a", { class: "row", href: `#/cases?id=${item.case_id || ""}`,
          style: "border:0;padding:7px 0;background:none" },
          el("span", { class: "avatar", text: initials(item.title) }),
          el("span", { class: "row-main" },
            el("span", { class: "row-title", text: item.title }),
            el("span", { class: "row-sub", text: humanise(item.detail) })),
          el("span", { class: "row-side" }, el("span", { class: "meta", text: "→" }))))),
      brief.stalled.length > worth.length
        ? el("div", { class: "meta", style: "margin-top:10px",
            text: `and ${brief.stalled.length - worth.length} more` })
        : null));
  }

  if (brief.deadlines.length) {
    nodes.push(el("h2", { class: "section", text: "Deadlines" }));
    nodes.push(el("div", { class: "stack" }, brief.deadlines.map((item) => {
      const tone = toneFor({ due: item.due });
      return el("a", { class: "card", "data-tone": tone, href: `#/cases?id=${item.case_id || ""}` },
        el("div", { class: "flag", "data-tone": tone, text: overdueWords(item.due) }),
        el("div", { class: "card-title", text: item.title }),
        el("div", { class: "card-org", text: dateWords(item.due) }));
    })));
  }

  if (counts.archived) {
    nodes.push(el("h2", { class: "section", text: "Filed away" }));
    nodes.push(el("div", { class: "card" },
      el("div", { class: "card-title", text: `${counts.archived} messages needed nothing from you.` }),
      el("div", { class: "card-org",
        text: "Newsletters, job alerts, automatic acknowledgements and the like." }),
      el("div", { class: "card-foot" },
        el("button", { class: "sm", onclick: () => go("mail") }, "Look through them"))));
  }

  // The window picker is meaningless before anything has been read, and an
  // empty first screen should carry exactly one thing to do.
  if (state.verdicts.total) {
    nodes.push(el("div", { class: "bar-row", style: "margin-top:36px" },
      el("span", { class: "meta", text: "Showing" }),
      el("select", { onchange: (e) => { localStorage.setItem("bmail.window", e.target.value); render(); } },
        [[1, "today"], [7, "the last week"], [30, "the last month"], [3650, "everything"]]
          .map(([value, label]) => el("option", { value, selected: value === days }, label)))));
  }

  paint(...nodes);
}

function headlineFor(counts, days, state) {
  const needs = counts.needs_you || 0;
  if (!state.verdicts.total) {
    return [
      el("h1", { class: "headline", text: "Nothing has been read yet." }),
      el("p", { class: "subhead",
        text: "Point Bmail at a mailbox and it will tell you what needs you, what has a deadline, and what has gone quiet." }),
      el("div", { class: "bar-row" },
        el("button", { class: "primary", onclick: () => go("setup") }, "Connect a mailbox")),
    ];
  }
  if (needs) {
    const extra = [];
    if (counts.deadlines) extra.push(`${counts.deadlines} ${counts.deadlines === 1 ? "has a deadline" : "have deadlines"}`);
    if (counts.events) extra.push(`${counts.events} ${counts.events === 1 ? "is" : "are"} in the diary`);
    return [
      el("h1", { class: "headline" }, countWords(needs), " ", el("em", { text: "need you" }), "."),
      extra.length ? el("p", { class: "subhead", text: `${extra.join(", and ")}.` }) : null,
    ];
  }
  const outstanding = state.verdicts.action_required || 0;
  if (outstanding && days < 3650) {
    return [
      el("h1", { class: "headline", text: "You are clear for this window." }),
      el("p", { class: "subhead", text: `${outstanding} things need you across everything Bmail has read.` }),
      el("div", { class: "bar-row" },
        el("button", { onclick: () => { localStorage.setItem("bmail.window", "3650"); render(); } },
          "Show me all of them")),
    ];
  }
  return [
    el("h1", { class: "headline", text: "You are all clear." }),
    el("p", { class: "subhead", text: "Nothing is waiting on you. That is the whole point." }),
  ];
}

function briefCard(item) {
  const tone = toneFor({ due: item.due, urgency: item.urgency, actionRequired: true });
  const org = [item.org, item.role].filter(Boolean).join(" · ") || item.detail;
  const category = humanise(item.category);
  return el("a", {
    class: "card", "data-tone": tone,
    href: item.message_id ? `#/mail?id=${encodeURIComponent(item.message_id)}` : `#/cases?id=${item.case_id || ""}`,
  },
    el("div", { class: "card-top" },
      item.due ? el("span", { class: "flag", "data-tone": tone, text: overdueWords(item.due) })
               : item.when ? el("span", { class: "flag", "data-tone": tone, text: whenWords(item.when) })
               : el("span", { class: "flag", "data-tone": tone, text: category || "Needs you" }),
      category && (item.due || item.when) ? el("span", { class: "chip", text: category }) : null),
    el("div", { class: "card-title", text: item.title }),
    el("div", { class: "card-org", text: org }),
    item.evidence ? el("blockquote", { class: "quote", text: `“${item.evidence}”` }) : null);
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
      "Gone quiet"),
    el("span", { class: "grow" }),
    el("span", { class: "meta", text: `${data.cases.length} ${noun}` }));

  if (!data.cases.length) {
    paint(el("h1", { class: "headline", text: noun[0].toUpperCase() + noun.slice(1) }), controls,
      el("div", { class: "panel" }, empty("○", "Nothing tracked yet",
        `Bmail opens a ${App.state.entity} the first time it sees mail about one.`,
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
          el("h2", { class: "section", text: `Closed · ${closed.length}` }),
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
    el("div", { class: "card-top" },
      el("span", { class: "flag", "data-tone": tone, text: humanise(c.state) }),
      c.stalled && silent
        ? el("span", { class: "chip", "data-tone": "quiet", text: `silent ${silent} days` }) : null,
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
      el("span", { class: "grow" }),
      el("span", { class: "meta",
        text: c.last_event_at ? agoWords(c.last_event_at) : `${c.message_count} messages` })),
    el("div", { class: "card-title", text: c.org || c.title || "Untitled" }),
    c.role ? el("div", { class: "card-org", text: c.role }) : null,
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
      el("span", { text: humanise(path[0] || "") }),
      el("span", { text: off ? `→ ${humanise(c.state)}` : humanise(c.state) })));
}

async function caseAside(caseId) {
  let data;
  try { data = await api(`/cases/${caseId}`); }
  catch (err) { return el("div", { class: "panel panel-pad" }, empty("!", "Not found", err.message)); }
  const c = data.case;
  const silent = c.last_inbound_at ? -daysFrom(c.last_inbound_at) : null;

  return el("div", { class: "panel panel-pad" },
    el("div", { class: "eyebrow", text: humanise(c.state) }),
    el("h3", { style: "margin:6px 0 0", text: "" }),
    el("div", { class: "card-title", style: "font-size:18px", text: c.org || c.title }),
    c.role ? el("div", { class: "card-org", text: c.role }) : null,

    el("h3", { text: "The facts" }),
    el("dl", { class: "facts" },
      fact("Opened", dateWords(c.opened_at)),
      fact("Last heard", c.last_inbound_at
        ? `${dateWords(c.last_inbound_at)} · ${agoWords(c.last_inbound_at)}` : "never"),
      silent !== null ? fact("Silence", `${silent} days`) : null,
      c.next_action !== "none" ? fact("Next", humanise(c.next_action)) : null,
      c.next_action_due ? fact("Due", `${c.next_action_due} · ${whenWords(c.next_action_due)}`) : null,
      c.event_datetime ? fact("Diary", dateWords(c.event_datetime, true)) : null,
      c.contact_person ? fact("Contact", c.contact_person) : null),

    el("h3", { text: "What happened" }),
    el("ul", { class: "timeline" }, (data.timeline || []).map((entry) =>
      el("li", {},
        el("div", { class: "when", text: `${dateWords(entry.date_utc)} · ${humanise(entry.category || "")}` }),
        el("div", { class: "what", text: entry.subject }),
        entry.evidence ? el("blockquote", { class: "quote", text: `“${entry.evidence}”` }) : null))),

    el("button", { class: "quiet sm", style: "margin-top:16px", onclick: () => go("cases") }, "Close"));
}

const fact = (key, value) => (value ? [el("dt", { text: key }), el("dd", { text: String(value) })] : null);

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
    type: "search", placeholder: "Search everything…", value: q,
    onkeydown: (e) => { if (e.key === "Enter") go("mail", { ...here(), q: e.target.value, offset: 0 }); },
  });

  const controls = el("div", { class: "bar-row" },
    search,
    el("select", { onchange: (e) => go("mail", { ...here(), category: e.target.value, offset: 0 }) },
      el("option", { value: "" }, "Every kind"),
      (App.state.categories || []).map((c) =>
        el("option", { value: c.name, selected: c.name === category }, humanise(c.name)))),
    el("button", { class: onlyAction ? "on" : "",
      onclick: () => go("mail", { ...here(), action: onlyAction ? "" : "1", offset: 0 }) }, "Needs me"));

  if (!data.messages.length) {
    paint(el("h1", { class: "headline", text: "Mail" }), controls,
      el("div", { class: "panel" }, empty("✉", q ? "No match" : "No mail yet",
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
    el("span", { class: "row-side" },
      el("span", { class: "meta", text: agoWords(m.date_utc) }),
      m.category ? el("span", { class: "chip", "data-tone": tone, text: humanise(m.category) }) : null));
}

async function mailAside(messageId) {
  let data;
  try { data = await api(`/messages/${encodeURIComponent(messageId)}`); }
  catch (err) { return el("div", { class: "panel panel-pad" }, empty("!", "Not found", err.message)); }
  const { message: m, verdict: v, case: kase } = data;

  return el("div", { class: "panel panel-pad" },
    el("div", { class: "eyebrow", text: dateWords(m.date, true) }),
    el("div", { class: "card-title", style: "font-size:18px;margin-top:6px", text: m.subject || "(no subject)" }),
    el("div", { class: "card-org", text: `${m.from.name || ""} ${m.from.address}`.trim() }),

    v && v.evidence
      ? el("div", {},
          el("h3", { text: v.evidence_ok === false ? "Evidence · not found in this email" : "Because it says" }),
          el("blockquote", { class: `quote${v.evidence_ok === false ? " bad" : ""}`, text: `“${v.evidence}”` }))
      : null,

    el("h3", { text: "What Bmail made of it" }),
    el("dl", { class: "facts" },
      v ? fact("Kind", humanise(v.category)) : null,
      v && v.action_required ? fact("Action", humanise(v.next_action)) : null,
      v && v.deadline ? fact("Deadline", `${v.deadline} · ${whenWords(v.deadline)}`) : null,
      v && v.event_datetime ? fact("Diary", dateWords(v.event_datetime, true)) : null,
      v ? fact("Decided by", `${humanise(v.stage)} · ${Math.round((v.confidence || 0) * 100)}% sure`) : null,
      kase ? fact("Part of", kase.title) : null,
      m.attachments.length ? fact("Attached", m.attachments.map((a) => a.filename).join(", ")) : null),

    v ? el("div", {},
      el("h3", { text: "Is that right?" }),
      correctionControls(messageId, v)) : null,

    el("h3", { text: "The message" }),
    el("div", { class: "body-text", text: m.body_text || "(no text)" }));
}

function correctionControls(messageId, verdict) {
  const picker = el("select", {}, (App.state.categories || []).map((c) =>
    el("option", { value: c.name, selected: c.name === verdict.category }, humanise(c.name))));
  const action = el("input", { type: "checkbox", checked: verdict.action_required });
  return el("div", {},
    el("div", { class: "setup-row" }, picker,
      el("label", { class: "check" }, action, " needs me"),
      el("button", { class: "primary sm", onclick: async () => {
        try {
          await post(`/correct/${encodeURIComponent(messageId)}`,
            { category: picker.value, action_required: action.checked });
          toast("Noted — Bmail will remember that.");
          render();
        } catch (err) { toast(err.message, true); }
      } }, "Save")));
}

/* --- Review ------------------------------------------------------------ */
/* A list of uncertain guesses is a chore. One at a time is a rhythm. */

async function viewReview() {
  const data = await api("/messages?review=1&limit=60");
  const queue = data.messages;

  if (!queue.length) {
    paint(el("h1", { class: "headline", text: "Nothing to check." }),
      el("p", { class: "subhead", text: "Bmail was sure about everything it read. It will ask when it is not." }));
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
    el("div", { class: "review-progress" },
      el("span", { text: `${done + 1} of ${total}` }),
      el("span", { class: "track" },
        el("span", { class: "fill", style: `width:${((done + 1) / total) * 100}%` }))),

    el("h1", { class: "headline", text: "Did Bmail get this right?" }),

    el("div", { class: "card", style: "margin-top:22px" },
      el("div", { class: "eyebrow", text: `${m.from.name || m.from.address} · ${dateWords(m.date)}` }),
      el("div", { class: "card-title", style: "margin-top:6px", text: m.subject || "(no subject)" }),
      el("div", { class: "body-text", style: "margin-top:12px;max-height:200px",
        text: m.body_text || "(no text)" })),

    el("div", { class: "guess" },
      el("div", {},
        el("div", { class: "meta", text: "Bmail thinks this is" }),
        el("div", { class: "card-title", text: humanise(v.category) })),
      el("div", { class: "meta", text: `${Math.round((v.confidence || 0) * 100)}% sure` })),

    el("div", { class: "setup-row" },
      el("button", { class: "primary", onclick: () => accept(v.category) }, "Yes, that's right"),
      el("button", { class: "quiet", onclick: next }, "Skip")),

    el("h3", { style: "margin-top:26px", text: "No — it's actually" }),
    el("div", { class: "choices" }, suggestions.map((c) =>
      el("button", { onclick: () => accept(c.name) }, humanise(c.name))))));
}

/* --- Setup -------------------------------------------------------------- */

function viewSetup() {
  const s = App.state;
  const path = el("input", { type: "text", placeholder: "~/mail/archive.mbox", style: "flex:1" });
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

  const group = (label, ...children) =>
    el("div", { class: "setup-group" }, el("div", { class: "lbl", text: label }),
      el("div", { class: "setup-row" }, ...children));

  paint(
    el("h1", { class: "headline", text: "Setup" }),
    el("p", { class: "subhead", text: "Everything here happens on this computer." }),

    el("h2", { class: "section", text: "Mail" }),
    el("div", { class: "panel panel-pad" },
      group("From an account you have configured", account,
        el("button", { onclick: () => run("ingest", { account: account.value || null }, "Collecting mail") },
          "Collect")),
      group("Or from a file on this machine", path,
        el("button", { onclick: () => path.value.trim()
          ? run("ingest", { path: path.value.trim(), account: "local" }, "Reading file")
          : toast("Give me a path first", true) }, "Read")),
      el("p", { class: "meta", style: "margin:10px 0 0",
        text: "An mbox, a Maildir, or a folder of .eml files. A Gmail Takeout export works." })),

    el("h2", { class: "section", text: "Sorting" }),
    el("div", { class: "panel panel-pad" },
      el("div", { class: "setup-row" },
        el("button", { class: "primary", onclick: () => run("triage", {}, "Sorting new mail") },
          "Sort what's new"),
        el("button", { onclick: () => run("triage", { all: true }, "Sorting everything") },
          "Start again from scratch")),
      el("p", { class: "meta", style: "margin:12px 0 0", text: s.llm.available
        ? (s.llm.pulled
          ? `Using ${s.llm.model} on this machine for the hard ones.`
          : `${s.llm.model} isn't downloaded yet — run: ollama pull ${s.llm.model}`)
        : "No local model running, so Bmail is using rules and the classifier only. That handles most mail." })),

    el("h2", { class: "section", text: "What you track" }),
    el("div", { class: "panel panel-pad" },
      group("Domain", packs),
      el("p", { class: "meta", style: "margin:10px 0 0",
        text: `${s.pack} follows ${s.entity_plural} through ${(s.states || []).length} stages.` })),

    el("h2", { class: "section", text: "Take it elsewhere" }),
    el("div", { class: "panel panel-pad" },
      el("div", { class: "setup-row" }, formats,
        el("button", { onclick: () => {
          const kinds = formats.map((f) => f.querySelector("input")).filter((i) => i.checked)
            .map((i) => i.value);
          if (!kinds.length) return toast("Pick a format first", true);
          run("export", { kinds }, "Writing files");
        } }, "Save a copy")),
      el("p", { class: "meta", style: "margin:10px 0 0",
        text: "A spreadsheet, or a calendar file of every deadline and interview." })),

    el("h2", { class: "section", text: "Where things live" }),
    el("div", { class: "panel panel-pad" },
      el("dl", { class: "facts" },
        fact("Database", s.database),
        fact("Folder", s.home),
        fact("Mail read", `${s.messages.total}`),
        fact("Sorted", `${s.verdicts.total}`),
        fact("Tracked", `${s.cases.total} ${s.entity_plural}`))),

    el("h2", { class: "section", text: "Shortcuts" }),
    el("div", { class: "panel panel-pad" },
      el("dl", { class: "facts" },
        [["1", "Today"], ["2", "Cases"], ["3", "Mail"], ["4", "Review"], ["5", "Setup"],
         ["r", "Reload"], ["s", "Sort new mail"]].map(([key, what]) =>
          [el("dt", {}, el("kbd", { text: key })), el("dd", { text: what })]))));
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
  App.route = route;
  if (route !== "review") App.reviewIndex = 0;
  paintNav();
  try {
    await ROUTES[route](new URLSearchParams(rawQuery || ""));
  } catch (err) {
    toast(err.message, true);
    paint(el("div", { class: "panel" }, empty("!", "That did not load", err.message,
      el("button", { onclick: () => render() }, "Try again"))));
  }
}

function paintNav() {
  const s = App.state;
  const review = s.verdicts.needs_review || 0;
  // A badge should mean "something is waiting for you", never "here is a
  // volume". A count of all mail tells nobody anything.
  const items = [
    ["today", "Today", "◧", s.verdicts.action_required || 0, "urgent"],
    ["cases", s.entity_plural[0].toUpperCase() + s.entity_plural.slice(1), "◇",
      s.cases.stalled || 0, "quiet"],
    ["mail", "Mail", "✉", 0, null],
    ["review", "Review", "?", review, "urgent"],
    ["setup", "Setup", "⚙", 0, null],
  ];
  $("#nav").replaceChildren(...items.map(([route, label, icon, count, flag]) =>
    el("a", {
      href: `#/${route}`, "data-tab": route, "data-flag": count ? flag : null,
      "aria-current": App.route === route ? "page" : null,
    },
      el("span", { class: "ico", text: icon }),
      el("span", { text: label }),
      count ? el("span", { class: "count", text: String(count) }) : null)));
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
      state.llm.available && state.llm.pulled ? `${state.llm.model}, on this machine` : "rules + classifier"));
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
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea") || e.metaKey || e.ctrlKey) return;
    const keys = { 1: "today", 2: "cases", 3: "mail", 4: "review", 5: "setup" };
    if (keys[e.key]) go(keys[e.key]);
    else if (e.key === "r") refresh();
    else if (e.key === "s") run("triage", {}, "Sorting new mail");
  });

  const jobs = await api("/jobs").catch(() => ({ jobs: [] }));
  const running = (jobs.jobs || []).find((j) => ["queued", "running"].includes(j.status));
  if (running) { paintJob(running); watchJob(running.id); }
}

if (!location.hash) location.hash = "#/today";
boot();
