# Domain packs

Job seekers, university applicants and recruiters are the same machine with
different vocabulary. A **pack** makes that explicit: it declares the categories
a domain cares about, what a "case" is, how cases are keyed, which states they
move through, and which categories are too expensive to get wrong.

The engine is domain-agnostic. Swapping `job-seeker` for `student-admissions`
changes the behaviour without a line of Python.

```bash
bmail packs                      # list
bmail packs student-admissions   # describe one
bmail --pack student-admissions triage
```

## Shipped packs

| Pack | Entity | States | What it is for |
|---|---|---|---|
| `job-seeker` | application | applied → … → offer / rejected | Ghosting detection, ATS template parsing, response-rate analytics |
| `student-admissions` | programme application | enquiry → submitted → docs_requested → interview → admitted / waitlist | Deadline extraction (uni-assist, DAAD, portal cutoffs), missing-document alerts |
| `hr-recruiter` | candidate × requisition | applied → screened → interview → offer | The mirror image: response-time SLAs instead of ghosting |
| `freelance-ops` | invoice | sent → viewed → overdue → paid | Amount and due-date extraction, an overdue chase list |

`student-admissions` covers a genuinely underserved case: international
applicants juggling eight to fifteen universities plus uni-assist, VPD numbers,
blocked accounts and APS certificates, each with hard deadlines and non-obvious
document requests.

## Writing one

A pack is one YAML file. Put it in a directory listed under `pack_dirs` in your
config, or pass its path directly.

```yaml
name: grant-applications
description: Research funding applications and reporting deadlines.
language: [en, de]

entity: grant_application
key: [org_domain, role]        # what makes two emails the same case
initial_state: submitted
states: [drafting, submitted, under_review, revisions, awarded, rejected, lapsed]
terminal_states: [awarded, rejected]
stall_days: 45

transitions:                   # time-based; no message carries the passage of time
  lapsed:
    from: [submitted, under_review]
    after_days_silent: 90

categories:
  revision_request:
    description: The funder asks for changes or more documents before deciding.
    action_required: true
    urgency: this_week
    next_action: submit_document
    high_stakes: true          # always escalate, whatever the classifier says
    state: revisions
    label: Revisions
    prototypes:                # seed phrases for the zero-shot cold start
      - "The committee requests a revised budget before the next meeting."
      - "Bitte reichen Sie eine überarbeitete Kostenaufstellung ein."

  other:
    description: Anything the pack does not model.

kpis: [award_rate, median_days_to_decision, deadlines_next_30d]
label_prefix: Bmail
```

### Fields

**Top level**

| Field | Meaning |
|---|---|
| `entity` | What a case *is*, for display |
| `key` | Fields combined into the case key. Organisation fields are normalised and despaced; role fields are fuzzy-matched at 85 |
| `states` | Ordered. Order defines "forward" — states never move backwards |
| `initial_state` | Where a newly opened case starts |
| `terminal_states` | Reaching one closes the case |
| `stall_days` | Silence beyond this marks the case stalled |
| `transitions` | Time-based transitions, evaluated on every run |
| `label_prefix` | Prefix for provider labels (`Bmail/Interview`) |

**Per category**

| Field | Meaning |
|---|---|
| `description` | Shown to the model, and used as a zero-shot prototype |
| `prototypes` | Seed phrases, ideally in every language you receive. These make day one work without labels |
| `action_required` / `urgency` / `next_action` | The policy for this category |
| `high_stakes` | Always escalate to the model, whatever the confidence. Use for anything catastrophic to miss |
| `state` | The state this category moves a case into |
| `opens_case` | May this category create a case? |
| `closes_case` | Does it close one? |
| `label` | Provider label suffix |

An `other` category is added automatically if you omit it. Packs are validated
on load: a category pointing at an unknown state is an error, not a surprise at
runtime.

### Getting the prototypes right

Prototypes carry the whole cold start, so write them as *sentences that would
actually appear in such an email*, in every language you receive — not as
descriptions of the category. Three or four good ones beat twenty vague ones.

```yaml
# good — real phrasings, both languages
prototypes:
  - "Unfortunately we have decided not to move forward with your application."
  - "Leider müssen wir Ihnen mitteilen, dass wir Ihre Bewerbung nicht berücksichtigen können."

# poor — a description, not a message
prototypes:
  - "emails about rejections"
```

Check what a new pack does before trusting it:

```bash
bmail --pack grant-applications triage --limit 50 --no-llm
bmail --pack grant-applications cases
```

## Recipes are separate

Packs declare *what a domain means*. Recipes (`bmail/rules/recipes/*.yaml`)
declare *how a specific sender writes*. A recipe names a category, so it only
fires for packs that declare that category — `bmail templates` reports coverage,
and the test suite fails if a recipe names a category no pack declares.

Adding a recipe for a system you meet is usually the highest-value change you
can make: it converts a model call into a regex.

```yaml
- id: my-portal-acknowledgement
  vendor: ExamplePortal
  category: acknowledgement
  priority: 70          # outcome recipes sit at 84–92; vendor acks at ~70
  match:                # every condition must hold; each is an any-of
    from_domain: [portal.example.edu]
    body_any: ["your application has been received"]
    body_none: ["we regret"]      # any hit vetoes the recipe
  extract:
    org:  {source: from_name, transform: [strip_via, strip, collapse]}
    role: {source: subject, regex: "for (.+)$", transform: [strip_org_suffix, strip]}
```

Point `recipe_dir` in your config at a directory of your own recipe files to
load them alongside the built-in set.
