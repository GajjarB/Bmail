# Architecture

Sift is a four-stage pipeline over a single SQLite file. Each stage is allowed
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

`sift/ingest/` holds one connector per provider behind a single interface
(`SourceConnector`). Provider APIs and auth policies churn yearly; assume each
connector gets rewritten once and keep the rewrite local.

Everything funnels through `sift/ingest/raw.py`, which produces the canonical
`Message` — including the API-based connectors, which fetch original MIME
(Gmail's `format=raw`, Graph's `$value`) rather than a provider's JSON
projection, so every source is parsed by identical code. Where a provider knows
the thread better than the headers do — Graph's `conversationId` — that is
passed in as an override.

Two details in the normaliser are load-bearing:

- **HTML → text.** Quoted history is the biggest source of noise in mail
  summarisation — a three-reply thread is typically 80% repetition. Bodies are
  extracted, then quoted chains and signatures are stripped before anything
  downstream sees them.
- **Threading via `References`/`In-Reply-To`.** Subject matching breaks across
  providers and languages (`Re:`, `AW:`, `Antw:`, `WG:`); the References chain
  does not. The root of the chain is the thread id.

Attachment payloads are counted, never stored.

## Stage 1 — deterministic (`sift/rules/`)

The stage most projects skip, and where the throughput comes from.

| Signal | Why it works |
|---|---|
| `List-Unsubscribe`, `Precedence`, `Auto-Submitted`, campaign headers | RFC-defined, machine-set, free to read |
| Contact graph | "Have I ever written to this address?" is the strongest single importance feature, and it is one indexed lookup |
| Template recipes | ATS mail is the same text with three fields swapped; once recognised, extraction is a regex — deterministic and instant |
| SimHash near-duplicate collapse | Folds "we received your application" ×40 into one row |

Order matters: templates run **before** bulk detection, so a recognised ATS mail
is not misfiled as bulk merely because it carries a `List-Unsubscribe` header.

Recipes are YAML (`sift/rules/recipes/`), matched most-specific-first. The
priority policy is deliberate: every ATS opens its *rejections* with "thank you
for your interest", so outcome recipes (offer, rejection, interview, assessment,
documents) rank above vendor acknowledgement recipes.

Run `sift templates` against your own corpus. That coverage number decides how
much model you actually need.

## Stage 2 — classify and route (`sift/classify/`)

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

## Stage 3 — local extraction (`sift/llm/`)

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

## Stage 4 — entities and state (`sift/entities/`)

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

## What is deliberately absent

- **No mail client.** No compose, no send, no rendering. That is a multi-year
  trap and it doubles the project the moment it starts.
- **No server.** There is nothing to run, nothing to breach, no annual audit.
- **No telemetry.** No analytics, no crash reporting, no update pings.
