/* Sift desktop UI.
 *
 * Deliberately framework-free. The page is served by the local daemon and has
 * to work with the machine offline, so there is no CDN, no bundler and no build
 * step — the file you read is the file that runs.
 *
 * The token arrives once in the URL (as `sift ui` opens it), is moved into
 * sessionStorage, and is stripped from the address bar so it never lands in
 * history or a screenshot.
 */

"use strict";

const App = {
  token: null,
  state: null,
  route: "brief",
  pollTimer: null,
  cache: {},
};

/* --- utilities ---------------------------------------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") throw new Error("refusing to set innerHTML");
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

function fmtDate(value, withTime = false) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 10);
  const opts = withTime
    ? { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "short", year: "numeric" };
  return d.toLocaleString(undefined, opts);
}

function relativeDays(iso) {
  if (!iso) return null;
  const days = Math.round((new Date(iso) - new Date()) / 86400000);
  if (Number.isNaN(days)) return null;
  if (days < 0) return `${-days} d overdue`;
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  return `in ${days} d`;
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", Boolean(isError));
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, isError ? 8000 : 3500);
}

/* --- API ----------------------------------------------------------------- */

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      "Authorization": `Bearer ${App.token}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  let payload = null;
  try { payload = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `HTTP ${response.status}`);
  }
  return payload;
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* --- jobs ---------------------------------------------------------------- */

async function startJob(kind, body) {
  try {
    const job = await post(`/${kind}`, body);
    showJob(job);
    pollJob(job.id);
  } catch (err) {
    toast(err.message, true);
  }
}

function showJob(job) {
  const bar = $("#job");
  if (!job || ["done", "error", "cancelled"].includes(job.status)) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  $("#job-kind").textContent = job.kind;
  $("#job-step").textContent = job.step || job.status;
  $("#job-fill").style.width = `${Math.round((job.progress || 0) * 100)}%`;
  $("#job-cancel").onclick = () => post(`/jobs/${job.id}/cancel`).catch(() => {});
}

function pollJob(jobId) {
  clearInterval(App.pollTimer);
  App.pollTimer = setInterval(async () => {
    let job;
    try {
      job = await api(`/jobs/${jobId}`);
    } catch {
      clearInterval(App.pollTimer);
      return;
    }
    showJob(job);
    if (["done", "error", "cancelled"].includes(job.status)) {
      clearInterval(App.pollTimer);
      if (job.status === "error") toast(`${job.kind} failed: ${job.error}`, true);
      else if (job.status === "cancelled") toast(`${job.kind} cancelled`);
      else toast(summariseJob(job));
      await refresh();
    }
  }, 500);
}

function summariseJob(job) {
  const r = job.result || {};
  if (job.kind === "triage") {
    return r.processed ? `Triaged ${r.processed} messages` : "Nothing new to triage";
  }
  if (job.kind === "ingest") {
    const total = Object.values(r.fetched || {}).reduce((a, b) => a + b, 0);
    return `Read ${total} messages`;
  }
  if (job.kind === "export") return `Wrote ${(r.files || []).length} file(s) to ${r.directory}`;
  return `${job.kind} finished`;
}

/* --- views ---------------------------------------------------------------- */

function setView(...nodes) {
  const view = $("#view");
  view.replaceChildren(el("div", { class: "wrap" }, ...nodes));
  view.scrollTop = 0;
}

function empty(title, hint, action) {
  return el("div", { class: "empty" }, el("strong", { text: title }), el("div", { text: hint }),
    action ? el("div", { style: "margin-top:14px" }, action) : null);
}

/* Brief ------------------------------------------------------------------- */

async function viewBrief() {
  const days = Number(localStorage.getItem("sift.briefDays") || 7);
  const brief = await api(`/brief?days=${days}&limit=10`);
  const c = brief.counts || {};

  const daySelect = el("select", {
    onchange: (e) => { localStorage.setItem("sift.briefDays", e.target.value); render(); },
  }, [1, 3, 7, 30, 120, 3650].map((d) =>
    el("option", { value: d, selected: d === days }, d === 3650 ? "all time" : `last ${d} d`)));

  const section = (title, items, renderer) =>
    items.length
      ? [el("h3", { text: title }), el("div", { class: "panel" }, items.map(renderer))]
      : [];

  setView(
    el("div", { class: "toolbar" },
      el("div", {}, el("h2", { text: "Brief" }),
        el("p", { class: "sub", text: `${c.needs_you || 0} need you · ${c.deadlines || 0} deadlines · ${c.stalled || 0} stalled · ${c.archived || 0} archived` })),
      el("span", { class: "grow" }), daySelect),

    el("div", { class: "cards" },
      card(c.needs_you || 0, "need you"),
      card(c.deadlines || 0, "deadlines"),
      card(c.events || 0, "upcoming"),
      card(c.stalled || 0, "stalled"),
      card(c.open_cases || 0, "open cases")),

    ...(brief.needs_you || []).length
      ? [el("h3", { text: "Needs you" }), el("div", { class: "panel" },
          brief.needs_you.map((item, i) => briefItem(item, i + 1)))]
      : [el("div", { class: "panel" }, emptyBrief(days))],

    ...section("Deadlines", brief.deadlines || [], (item) => caseLine(item)),
    ...section("Upcoming", brief.events || [], (item) => caseLine(item)),
    ...section("Stalled — worth one follow-up", brief.stalled || [], (item) => caseLine(item)),
    ...section("Archived", (brief.archived || []).slice(0, 12), (item) =>
      el("div", { class: "item" }, el("div", { class: "m", text: item.title }))),
  );
}

/* An empty Brief is the worst possible first impression, and on a freshly
 * imported archive it is also the most likely one — the window is short and the
 * mail is old. So say what is outside the window and offer one click to see it. */
function emptyBrief(days) {
  const outstanding = (App.state && App.state.verdicts && App.state.verdicts.action_required) || 0;
  const triaged = (App.state && App.state.verdicts && App.state.verdicts.total) || 0;
  if (!triaged) {
    return empty("Nothing triaged yet",
      "Read some mail, then run a triage — both live in Settings.",
      el("button", { class: "primary", onclick: () => go("settings") }, "Open Settings"));
  }
  if (outstanding && days < 3650) {
    return empty(`Nothing needs you in the last ${days} days`,
      `${outstanding} messages need action across the whole store.`,
      el("button", { class: "primary", onclick: () => {
        localStorage.setItem("sift.briefDays", "3650"); render();
      } }, "Show all time"));
  }
  return empty("Nothing needs you", "Everything here is filed. That is the point.");
}

const card = (n, k) => el("div", { class: "card" },
  el("div", { class: "n", text: String(n) }), el("div", { class: "k", text: k }));

function briefItem(item, rank) {
  const tags = [];
  if (item.due) tags.push(el("span", { class: "tag due", text: `deadline ${relativeDays(item.due)}` }));
  if (item.when) tags.push(el("span", { class: "tag", text: fmtDate(item.when, true) }));
  return el("a", { class: "item", href: item.message_id ? `#/messages?id=${encodeURIComponent(item.message_id)}` : `#/cases?id=${item.case_id}` },
    el("div", { class: "t" }, el("span", { class: "rank", text: `${rank}.` }), item.title),
    el("div", { class: "m" }, item.detail, " ", ...tags),
    item.evidence ? el("div", { class: "evidence", text: item.evidence }) : null);
}

const caseLine = (item) => el("a", { class: "item", href: `#/cases?id=${item.case_id || ""}` },
  el("div", { class: "t", text: item.title }), el("div", { class: "m", text: item.detail }));

/* Cases -------------------------------------------------------------------- */

async function viewCases(params) {
  const stateFilter = params.get("state") || "";
  const onlyStalled = params.get("stalled") === "1";
  const query = new URLSearchParams();
  if (stateFilter) query.set("state", stateFilter);
  if (onlyStalled) query.set("stalled", "1");
  const data = await api(`/cases?${query}`);
  const selected = params.get("id");

  const filters = el("div", { class: "toolbar" },
    el("h2", { text: "Cases" }), el("span", { class: "grow" }),
    el("select", { onchange: (e) => go("cases", e.target.value ? { state: e.target.value } : {}) },
      el("option", { value: "" }, "every state"),
      (data.states || []).map((s) => el("option", { value: s, selected: s === stateFilter }, s))),
    el("button", { class: onlyStalled ? "primary small" : "small",
      onclick: () => go("cases", onlyStalled ? {} : { stalled: "1" }) }, "Stalled only"));

  if (!data.cases.length) {
    setView(filters, el("div", { class: "panel" },
      empty("No cases yet", "Cases appear once mail has been triaged.")));
    return;
  }

  const rows = data.cases.map((c) => el("tr", { onclick: () => go("cases", { id: c.id }) },
    el("td", { class: "num", text: `#${c.id}` }),
    el("td", {}, el("span", { class: "tag" + (c.stalled ? " due" : "") , text: c.state })),
    el("td", { text: c.org || "—" }),
    el("td", { text: c.role || "—" }),
    el("td", { class: "num", text: String(c.message_count) }),
    el("td", { class: "num", text: c.last_event_at ? fmtDate(c.last_event_at) : "—" })));

  const table = el("div", { class: "panel" }, el("table", {},
    el("thead", {}, el("tr", {}, ["", "state", "organisation", "role", "msgs", "last"].map((h) => el("th", { text: h })))),
    el("tbody", {}, rows)));

  setView(filters, selected
    ? el("div", { class: "split" }, table, el("div", { class: "detail" }, await caseDetail(selected)))
    : table);
}

async function caseDetail(caseId) {
  let data;
  try { data = await api(`/cases/${caseId}`); }
  catch (err) { return el("div", { class: "panel" }, empty("Not found", err.message)); }
  const c = data.case;
  return el("div", { class: "panel" },
    el("div", { style: "padding:15px" },
      el("h2", { text: c.title || `Case ${c.id}` }),
      el("dl", { class: "kv" },
        kv("State", c.state), kv("Organisation", c.org), kv("Role", c.role),
        kv("Opened", fmtDate(c.opened_at)), kv("Last event", fmtDate(c.last_event_at)),
        kv("Next action", c.next_action),
        c.next_action_due ? kv("Due", `${c.next_action_due} (${relativeDays(c.next_action_due)})`) : null,
        c.event_datetime ? kv("Event", fmtDate(c.event_datetime, true)) : null,
        kv("Messages", String(c.message_count)),
        c.stalled ? kv("Stalled", "yes — worth one follow-up") : null)),
    el("ul", { class: "timeline" }, (data.timeline || []).map((entry) =>
      el("li", {},
        el("div", { class: "m" }, fmtDate(entry.date_utc), " · ",
          el("span", { class: "tag", text: entry.category || "—" })),
        el("div", { class: "t", text: entry.subject }),
        entry.evidence ? el("div", { class: "evidence", text: entry.evidence }) : null))));
}

const kv = (k, v) => v ? [el("dt", { text: k }), el("dd", { text: String(v) })] : null;

/* Messages ------------------------------------------------------------------ */

async function viewMessages(params, opts = {}) {
  const q = params.get("q") || "";
  const category = params.get("category") || "";
  const onlyAction = params.get("action") === "1";
  const offset = Number(params.get("offset") || 0);
  const query = new URLSearchParams({ limit: "50", offset: String(offset) });
  if (q) query.set("q", q);
  if (category) query.set("category", category);
  if (onlyAction) query.set("action", "1");
  if (opts.review) query.set("review", "1");

  const data = await api(`/messages?${query}`);
  const selected = params.get("id");
  const categories = (App.state.categories || []).map((c) => c.name);

  const search = el("input", {
    type: "search", placeholder: "Search subject and body…", value: q,
    onkeydown: (e) => { if (e.key === "Enter") go(App.route, { ...current(), q: e.target.value, offset: 0 }); },
  });

  const toolbar = el("div", { class: "toolbar" },
    el("h2", { text: opts.review ? "Review" : "Messages" }),
    search,
    el("select", { onchange: (e) => go(App.route, { ...current(), category: e.target.value, offset: 0 }) },
      el("option", { value: "" }, "every category"),
      categories.map((c) => el("option", { value: c, selected: c === category }, c))),
    el("button", { class: onlyAction ? "primary small" : "small",
      onclick: () => go(App.route, { ...current(), action: onlyAction ? "" : "1", offset: 0 }) },
      "Needs action"));

  if (!data.messages.length) {
    setView(toolbar, el("div", { class: "panel" }, opts.review
      ? empty("Nothing to review", "Low-confidence verdicts appear here after a triage run.")
      : empty("No messages", q ? "No match for that search." : "Ingest some mail to get started.")));
    return;
  }

  const rows = data.messages.map((m) => el("tr", { onclick: () => go(App.route, { ...current(), id: m.message_id }) },
    el("td", { class: "num", text: fmtDate(m.date_utc) }),
    el("td", { text: m.from_name || m.from_addr }),
    el("td", {}, el("div", { class: "t", text: m.subject || "(no subject)" }),
      m.summary ? el("div", { class: "m", text: m.summary }) : null),
    el("td", {}, m.category ? el("span", { class: "tag" + (m.action_required ? " act" : ""), text: m.category }) : el("span", { class: "muted", text: "untriaged" })),
    el("td", { class: "num" }, m.needs_review ? el("span", { class: "tag due", text: "review" }) : (m.confidence != null ? m.confidence.toFixed(2) : ""))));

  const pager = el("div", { class: "toolbar", style: "margin-top:12px" },
    el("span", { class: "muted", text: `${offset + 1}–${offset + data.messages.length} of ${data.total}` }),
    el("span", { class: "grow" }),
    el("button", { class: "small", disabled: offset === 0,
      onclick: () => go(App.route, { ...current(), offset: Math.max(0, offset - 50) }) }, "← previous"),
    el("button", { class: "small", disabled: offset + 50 >= data.total,
      onclick: () => go(App.route, { ...current(), offset: offset + 50 }) }, "next →"));

  const table = el("div", {}, el("div", { class: "panel" }, el("table", {},
    el("thead", {}, el("tr", {}, ["date", "from", "subject", "category", ""].map((h) => el("th", { text: h })))),
    el("tbody", {}, rows))), pager);

  setView(toolbar, selected
    ? el("div", { class: "split" }, table, el("div", { class: "detail" }, await messageDetail(selected)))
    : table);
}

async function messageDetail(messageId) {
  let data;
  try { data = await api(`/messages/${encodeURIComponent(messageId)}`); }
  catch (err) { return el("div", { class: "panel" }, empty("Not found", err.message)); }
  const { message: m, verdict: v, case: kase } = data;

  const categories = (App.state.categories || []).map((c) => c.name);
  const picker = el("select", {}, categories.map((c) =>
    el("option", { value: c, selected: v && c === v.category }, c)));
  const actionBox = el("input", { type: "checkbox", checked: v ? v.action_required : false });

  const save = el("button", { class: "primary small", onclick: async () => {
    try {
      await post(`/correct/${encodeURIComponent(messageId)}`,
        { category: picker.value, action_required: actionBox.checked });
      toast("Correction saved — the classifier will learn from it");
      render();
    } catch (err) { toast(err.message, true); }
  } }, "Save correction");

  return el("div", { class: "panel" }, el("div", { style: "padding:15px" },
    el("h2", { text: m.subject || "(no subject)" }),
    el("dl", { class: "kv" },
      kv("From", `${m.from.name} <${m.from.address}>`.trim()),
      kv("Date", fmtDate(m.date, true)),
      kv("Language", m.language),
      kv("Account", `${m.account} (${m.provider})`),
      v ? kv("Category", `${v.category} · ${v.stage} · confidence ${(v.confidence ?? 0).toFixed(2)}`) : null,
      v && v.urgency !== "none" ? kv("Urgency", v.urgency) : null,
      v && v.deadline ? kv("Deadline", `${v.deadline} (${relativeDays(v.deadline)})`) : null,
      v && v.event_datetime ? kv("Event", fmtDate(v.event_datetime, true)) : null,
      v && v.org ? kv("Organisation", v.org) : null,
      v && v.role ? kv("Role", v.role) : null,
      kase ? kv("Case", `#${kase.id} ${kase.title}`) : null,
      m.attachments.length ? kv("Attachments", m.attachments.map((a) => a.filename).join(", ")) : null),

    v && v.evidence
      ? el("div", {}, el("h3", { text: `Evidence${v.evidence_ok === false ? " — NOT FOUND IN THE EMAIL" : ""}` }),
          el("div", { class: "evidence", text: v.evidence }))
      : null,

    v && v.reasons && v.reasons.length
      ? el("div", {}, el("h3", { text: "Why" }),
          el("ul", { class: "m" }, v.reasons.map((r) => el("li", { text: r }))))
      : null,

    el("h3", { text: "Correct the verdict" }),
    el("div", { class: "row" }, picker,
      el("label", { class: "row" }, actionBox, " needs action"), save),

    el("h3", { text: "Message" }),
    el("div", { class: "body", text: m.body_text || "(empty body)" })));
}

/* Settings ------------------------------------------------------------------ */

function viewSettings() {
  const s = App.state;
  const packPicker = el("select", { onchange: (e) => {
    localStorage.setItem("sift.pack", e.target.value); location.reload();
  } }, (s.packs || []).map((p) => el("option", { value: p, selected: p === s.pack }, p)));

  const ingestPath = el("input", { type: "text", placeholder: "/path/to/mail.mbox", style: "flex:1" });
  const accountPicker = el("select", {},
    el("option", { value: "" }, "— configured account —"),
    (s.configured_accounts || []).map((a) => el("option", { value: a.name }, `${a.name} (${a.provider})`)));

  const exportKinds = ["xlsx", "csv", "ics"].map((k) =>
    el("label", { class: "row" }, el("input", { type: "checkbox", value: k, checked: k === "xlsx" }), " ", k));

  setView(
    el("h2", { text: "Settings" }),
    el("p", { class: "sub", text: "Everything here runs on this machine." }),

    el("h3", { text: "Pack" }),
    el("div", { class: "panel" }, el("div", { style: "padding:15px" },
      el("div", { class: "field" }, el("label", { text: "Active domain pack" }), packPicker),
      el("p", { class: "muted", text: `${(s.categories || []).length} categories, ${(s.states || []).length} states` }))),

    el("h3", { text: "Ingest" }),
    el("div", { class: "panel" }, el("div", { style: "padding:15px" },
      el("div", { class: "field" }, el("label", { text: "From a configured account" }),
        el("div", { class: "row" }, accountPicker,
          el("button", { onclick: () => startJob("ingest", { account: accountPicker.value || null }) }, "Read mail"))),
      el("div", { class: "field" }, el("label", { text: "Or from an mbox / Maildir / .eml directory" }),
        el("div", { class: "row" }, ingestPath,
          el("button", { onclick: () => ingestPath.value.trim()
            ? startJob("ingest", { path: ingestPath.value.trim(), account: "local" })
            : toast("Enter a path first", true) }, "Read file"))))),

    el("h3", { text: "Triage" }),
    el("div", { class: "panel" }, el("div", { style: "padding:15px" },
      el("div", { class: "row" },
        el("button", { class: "primary", onclick: () => startJob("triage", {}) }, "Triage new mail"),
        el("button", { onclick: () => startJob("triage", { all: true }) }, "Re-triage everything"),
        el("button", { onclick: () => startJob("triage", { no_llm: true }) }, "Rules + classifier only")),
      el("p", { class: "muted", text: s.llm.available
        ? (s.llm.pulled ? `Local model ready: ${s.llm.model}` : `Model ${s.llm.model} is not pulled — run: ollama pull ${s.llm.model}`)
        : `No local model backend at ${s.llm.host}. Rules and the classifier still work.` }))),

    el("h3", { text: "Export" }),
    el("div", { class: "panel" }, el("div", { style: "padding:15px" },
      el("div", { class: "row" }, exportKinds,
        el("button", { onclick: () => {
          const kinds = exportKinds.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
          if (!kinds.length) return toast("Pick at least one format", true);
          startJob("export", { kinds });
        } }, "Export")))),

    el("h3", { text: "Store" }),
    el("div", { class: "panel" }, el("div", { style: "padding:15px" },
      el("dl", { class: "kv" },
        kv("Database", s.database), kv("Data directory", s.home),
        kv("Messages", `${s.messages.total} (${s.messages.inbound} inbound)`),
        kv("Triaged", String(s.verdicts.total)),
        kv("By stage", JSON.stringify(s.verdicts.by_stage)),
        kv("Cases", `${s.cases.total} (${s.cases.open} open, ${s.cases.stalled} stalled)`)))),
  );
}

/* --- routing ---------------------------------------------------------------- */

function current() {
  return Object.fromEntries(new URLSearchParams(location.hash.split("?")[1] || ""));
}

function go(route, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined));
  location.hash = `#/${route}${query.toString() ? `?${query}` : ""}`;
}

async function render() {
  const [path, rawQuery] = location.hash.replace(/^#\/?/, "").split("?");
  const route = path || "brief";
  App.route = route;
  const params = new URLSearchParams(rawQuery || "");

  for (const tab of document.querySelectorAll(".tabs a")) {
    tab.setAttribute("aria-selected", tab.dataset.tab === route ? "true" : "false");
  }

  try {
    if (route === "brief") await viewBrief();
    else if (route === "cases") await viewCases(params);
    else if (route === "messages") await viewMessages(params);
    else if (route === "review") await viewMessages(params, { review: true });
    else if (route === "settings") viewSettings();
    else go("brief");
  } catch (err) {
    toast(err.message, true);
    setView(el("div", { class: "panel" }, empty("Could not load this view", err.message)));
  }
}

async function refresh() {
  const pack = localStorage.getItem("sift.pack");
  App.state = await api(`/state${pack ? `?pack=${encodeURIComponent(pack)}` : ""}`);
  const s = App.state;
  $("#pack-label").textContent = s.pack;
  $("#status-store").textContent =
    `${s.messages.total} messages · ${s.verdicts.total} triaged · ${s.cases.total} cases`;
  $("#status-llm").textContent = s.llm.available
    ? `model ${s.llm.model}${s.llm.pulled ? "" : " (not pulled)"}`
    : "rules + classifier";
  const review = s.verdicts.needs_review || 0;
  const badge = $("#review-count");
  badge.textContent = String(review);
  badge.hidden = review === 0;
  await render();
}

/* --- boot -------------------------------------------------------------------- */

async function boot() {
  const url = new URL(location.href);
  const supplied = url.searchParams.get("token");
  if (supplied) {
    sessionStorage.setItem("sift.token", supplied);
    // Keep the token out of the address bar, history and any screenshot.
    url.searchParams.delete("token");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  App.token = sessionStorage.getItem("sift.token");

  if (!App.token) {
    $("#boot-msg").replaceChildren(
      document.createTextNode("This page needs the session token. Start it with:"),
      el("code", { text: "sift ui" }));
    return;
  }

  try {
    await refresh();
  } catch (err) {
    $("#boot-msg").replaceChildren(
      document.createTextNode(`Could not reach the Sift daemon: ${err.message}`),
      el("code", { text: "sift ui" }));
    return;
  }

  $("#boot").hidden = true;
  $("#shell").hidden = false;

  window.addEventListener("hashchange", render);
  $("#btn-refresh").onclick = () => refresh();
  $("#btn-triage").onclick = () => startJob("triage", {});
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea")) return;
    const keys = { "1": "brief", "2": "cases", "3": "messages", "4": "review", "5": "settings" };
    if (keys[e.key]) go(keys[e.key]);
    else if (e.key === "r") refresh();
    else if (e.key === "t") startJob("triage", {});
  });

  // Reattach to a job that was already running when the page loaded.
  const jobs = await api("/jobs").catch(() => ({ jobs: [] }));
  const running = (jobs.jobs || []).find((j) => ["queued", "running"].includes(j.status));
  if (running) { showJob(running); pollJob(running.id); }
}

if (!location.hash) location.hash = "#/brief";
boot();
