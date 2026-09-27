# Architecture

Bmail is a four-stage pipeline over a single SQLite file. Each stage is allowed
to settle a message and stop; only what survives reaches the next one. That
cascade is the whole cost model.

```
  Gmail API (readonly + historyId) ─┐
  Microsoft Graph (delta)          ─┤
  IMAP (UIDVALIDITY + UID)         ─┼──▶ Normaliser ──▶ canonical Message
  mbox / Maildir / .eml            ─┘

  Message
    │
    ├─ Stage 1  deterministic: headers, contact graph, template recipes, dedupe
    │             └── ~70% resolved, zero model calls ──────────────┐
    ├─ Stage 2  embeddings + calibrated classifier                  │
    │             ├── high confidence ──────────────────────────────┤
    │             └── low confidence or high stakes                 │
    ├─ Stage 3  local LLM, schema-constrained, evidence spans        │
    │                                                               │
    └─ Stage 4  entity resolution + state machine ───────────────────┤
                                                                    ▼
                                        SQLite (FTS5) ──▶ Brief / Cases / Exports
                                              ▲                       │
                                              └──── corrections ──────┘
```

## Stage 0 — ingest and normalise

`bmail/ingest/` holds one connector per provider behind a single interface
(`SourceConnector`). Provider APIs and auth policies churn yearly; assume each
connector gets rewritten once and keep the rewrite local.

Everything funnels through `bmail/ingest/raw.py`, which produces the canonical
`Message` — including the API-based connectors, which fetch original MIME
(Gmail's `format=raw`, Graph's `$value`) rather than a provider's JSON
projection, so every source is parsed by identical code. Where a provider knows
the thread better than the headers do — Graph's `conversationId` — that is
passed in as an override.

Four details in the normaliser are load-bearing:

- **HTML → text.** The text has to read the way the mail looks in a mail
  client, because the classifier reads its first few hundred characters and
  the app shows it to a person. So what a client hides stays hidden (the
  `display:none` preheader, padded with invisible characters), paragraphs,
  bullets and table rows survive as plain text ("Date · Tuesday"), and inline
  markup never splits a word. The HTML part is read instead of the plain one
  when the plain one is a "view this email in your browser" stub, carries a
  stylesheet, is mostly tracking links, or says much less (counted in words,
  not characters, so links do not count as content); a part in an unknown
  charset is decoded rather than dropped.
- **What a person reads.** CSS that a sender's software left in the plain
  text is removed, a long link shows where it goes ("netflix.com/title/…")
  instead of its tracking code, and ruler lines are dropped. The text as it
  came out of the message is kept beside the cleaned one, so when these rules
  improve, mail already in the store is cleaned again the next time it opens.
- **Quoted history.** The biggest source of noise in mail summarisation — a
  three-reply thread is typically 80% repetition. Quoted chains, signatures
  and "view in browser" lines are stripped before anything downstream sees
  them; the full text is kept beside it for the reader who wants it.
- **Threading via `References`/`In-Reply-To`.** Subject matching breaks across
  providers and languages (`Re:`, `AW:`, `Antw:`, `WG:`); the References chain
  does not. The root of the chain is the thread id.

Attachment payloads are counted, never stored.

## Stage 1 — deterministic (`bmail/rules/`)

The stage most projects skip, and where the throughput comes from.

| Signal | Why it works |
|---|---|
| `List-Unsubscribe`, `Precedence`, `Auto-Submitted`, campaign headers | RFC-defined, machine-set, free to read |
| Contact graph | "Have I ever written to this address?" is the strongest single importance feature, and it is one indexed lookup |
| Template recipes | ATS mail is the same text with three fields swapped; once recognised, extraction is a regex — deterministic and instant |
| SimHash near-duplicate collapse | Folds "we received your application" ×40 into one row |

Order matters: templates run **before** bulk detection, so a recognised ATS mail
is not misfiled as bulk merely because it carries a `List-Unsubscribe` header.

Recipes are YAML (`bmail/rules/recipes/`), matched most-specific-first. The
priority policy is deliberate: every ATS opens its *rejections* with "thank you
for your interest", so outcome recipes (offer, rejection, interview, assessment,
documents) rank above vendor acknowledgement recipes.

Run `bmail templates` against your own corpus. That coverage number decides how
much model you actually need.

## Stage 2 — classify and route (`bmail/classify/`)

Vectorise `subject + sender + first ~600 characters`, then two heads:

- `PrototypeHead` — nearest class prototype, seeded from the phrases each pack
  declares. Works with zero labelled data, which is the state you are in on day
  one. Every correction folds into the centroid, so it becomes personal within a
  week — something a cloud product cannot do cheaply.
- `LogisticHead` — trained on confirmed labels once you have them, calibrated by
  temperature scaling on a held-out split.

The routing rule is asymmetric on purpose:

```
high confidence            -> accept
low confidence             -> escalate to the model
high-stakes category       -> escalate regardless of confidence
```

Missing an interview invitation is catastrophic; a false alarm costs three
seconds. Packs declare which categories are in the expensive-to-miss set.

Calibration is not decoration: a head that reports 0.97 for everything cannot
decide what to escalate, and confidence routing quietly stops working.

## Stage 3 — local extraction (`bmail/llm/`)

Ollama, `format=<JSON Schema>`, temperature 0, two calls per message.

Constrained decoding guarantees **schema validity and nothing about value
correctness**. Four consequences, all implemented:

1. **Small, flat schemas** — ten fields, no nesting. Value accuracy degrades as
   schemas grow deeper.
2. **Split the tasks** — classification and extraction are separate calls.
3. **Validate afterwards** — Pydantic on the way out; a valid
   `"deadline": "not mentioned"` is still garbage. One repair retry, then `null`
   plus a review flag.
4. **Demand an evidence span** — every extracted date arrives with the sentence
   it came from, and that sentence is checked against the email. A date the
   email does not contain is dropped, not shown.

That last one is the highest-value decision in the project: it makes the UI
trustworthy ("interview 14 September — *because this line says so*") and turns
hallucination into a measurable number instead of a silent failure.

### Security

Email is attacker-controlled text being fed to a model, so prompt injection is
not hypothetical. Three defences:

- **The model has no tools.** It emits JSON. Every side effect happens in Python
  from validated fields. There is no action it can reach.
- **The body is data.** It is fenced with delimiters that are stripped from the
  content first, and the system prompt says the fenced region is untrusted.
- **Nothing is fetched.** URLs are reduced to their host before the text reaches
  a prompt — no tracking pixels, no SSRF.

Nothing auto-deletes, ever. The worst case for a triage mistake is a wrong row
in a local database.

## Stage 4 — entities and state (`bmail/entities/`)

What makes this a tracker rather than a summariser.

- **Matching**, in order of reliability: the References chain, then the case key
  (normalised organisation + fuzzy role via token-set ratio, threshold 85), then
  open a new case.
- **Organisations**: a mail from `no-reply@greenhouse.io` is not a mail from
  Greenhouse, and one from `jonas@gmx.de` is not from GMX. Sending platforms and
  consumer mailboxes never become employers.
- **Organisation keys drop whitespace**, so "Atlas Payments" from a display name
  and "atlaspayments" from a domain land on one case.
- **States move forward only.** A late acknowledgement cannot undo an offer.
- **Two guards on case creation**: an uncertain verdict never mints an entity,
  and neither does a message with no identifiable organisation. Attaching to an
  existing case is still allowed — only creation is blocked.
- **Time-based transitions** (`ghosted`, `overdue`, `unresponsive`) are
  recomputed on every run, because no message carries the passage of time.

## Storage

One SQLite file. FTS5 for search; `sqlite-vec` is the intended home for
embeddings when the vector count justifies it.

A spreadsheet is a fine *export* and a terrible *source of truth*: it cannot
dedupe, cannot hold a state machine, and cannot tell you what has gone stale.

## The UI seam

The core is headless and importable. `bmail.api` puts a loopback HTTP server in
front of it and `bmail/ui/static` is a page that consumes it; the Tauri shell in
`desktop/` owns a window and a child process and nothing more.

That server was originally read-only, on the argument that ingest and triage are
long-running jobs belonging to a scheduler rather than an HTTP handler. The UI
made that untenable — a person clicking "Triage" should not be told to open a
terminal — so writes exist now, but never inline: they queue on the single
worker in `bmail.jobs`, and the page polls for progress. One worker, not a pool,
because the store has one writer and two triage runs at once is never what
anyone wanted. The handler itself still does nothing slower than a query.

Details, including the five guards that make running a mail-reading server on a
laptop defensible, are in [UI.md](UI.md).

## What is deliberately absent

- **No mail client.** No compose, no send, no rendering. That is a multi-year
  trap and it doubles the project the moment it starts.
- **No remote server.** The only server is a loopback daemon on your own
  machine, started and stopped with the window. Nothing to breach remotely, no
  annual audit.
- **No telemetry.** No analytics, no crash reporting, no update pings.
