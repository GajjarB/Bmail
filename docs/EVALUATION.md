# Evaluation

Build this in week 5, not week 20. It is what separates a demo from something
you can trust with an interview invitation, and it is the part that makes the
project defensible as a study.

```bash
bmail eval labels.jsonl                 # locked test split
bmail eval labels.jsonl --ablate        # the configuration comparison
bmail eval labels.jsonl --describe      # dataset composition only
bmail eval labels.jsonl --out report.json
```

## The dataset

Hand-label 500–800 of your own messages. Stratify roughly:

| Slice | Share |
|---|---|
| Job or admissions lifecycle | 40% |
| Admin and university | 20% |
| Newsletters and bulk | 20% |
| Personal | 10% |
| Edge cases (multilingual, forwarded, calendar invites, HTML-only) | 10% |

One JSON object per line. Only `message_id` is required; every other field is
optional and simply excluded from the metrics that need it.

```json
{"message_id": "<abc@mail>", "category": "interview_invite", "action_required": true,
 "deadline": null, "event_datetime": "2026-09-14T10:00:00+02:00", "language": "de"}
```

The test split is derived from a hash of the message id, not a shuffle, so it is
stable across runs and machines. A split you can accidentally reshuffle is a
split you will accidentally train on.

Evaluation runs inside a savepoint that is rolled back, so scoring a
configuration never contaminates the live store.

`bmail samples` writes a synthetic corpus with ground-truth labels, which is
enough to exercise the harness end to end. It is not a substitute for your own
mail — real corpora are messier in ways no generator invents.

## Metrics, and why these ones

**Recall on `action_required` is the headline number**, not macro-F1. Missing an
interview invitation is catastrophic; a false alarm costs three seconds. Report
the operating point, not the area under a curve.

**Precision@5 on the Brief** is the product metric. If the top five items each
morning are not right, no macro-F1 rescues it.

**Field accuracy, split by type.** Exact match on dates *including the
timezone* — an interview time that is an hour out is a failure, not a rounding
error. Fuzzy match on organisation names, because "Acme GmbH" and "Acme" are the
same employer and pretending otherwise measures the wrong thing.

**Hallucination rate** — the fraction of extracted values whose evidence span
does not appear in the email. Cheap to compute automatically, and the number
that decides whether output can be trusted without checking it.

**Cost** — p50/p95 latency per message, model calls, tokens. Accuracy without
cost is half a comparison.

## Ablations

`--ablate` runs the configurations that isolate one decision each:

| Configuration | The question it answers |
|---|---|
| 1. rules only | How far do headers, the contact graph and templates get alone? |
| 2. rules + classifier | What does a cheap local embedding head add? |
| 3. rules + classifier + LLM | The full hybrid |
| 4. LLM on everything | The naive baseline most projects build first — compared on cost as well as accuracy |
| 5. German only / English only | Multilingual degradation, which is real and under-reported |

Sample output on the synthetic corpus, rules and classifier only (no local
model), on the locked test split:

```
configuration                   acc  macroF1  act.recall  missed   p50ms   p95ms  calls
---------------------------------------------------------------------------------------
1. rules only                 0.923    0.889       0.818       2       1       2      0
2. rules + classifier         1.000    1.000       1.000       0       1       1      0
5. full · de only             1.000    1.000       1.000       0       1       2      0
5. full · en only             1.000    1.000       1.000       0       0       1      0
```

It was not always this: an earlier run read 0.727 action recall, and the
harness showed why. Every personal message was filed as an automatic
acknowledgement — with no local model, an unsure day-one guess was final — and
an English "your application is incomplete" had no template, while the German
one did. Both are fixed, and each fix has a test.

Treat a perfect score on the synthetic corpus as the floor, not the finish. It
is generated from the same patterns the rules were written against; your own
mail is the real test. Label a hundred of your own messages and run the harness
on those — the language split in the last two rows is where a system built and
tested in English quietly fails its German-speaking user.

## Ablations worth running by hand

The harness covers configurations. These need a model and a little scripting:

1. **Model size sweep** — 1B / 4B / 8B / 24B on identical prompts. Set
   `llm.model` in the config and re-run `bmail eval`.
2. **Quantisation** — Q4_K_M vs Q8 vs FP16. Ask specifically whether Q4 degrades
   *date extraction*, not just the aggregate.
3. **Constrained decoding on/off** — test on your own domain whether schema
   enforcement helps validity but costs value accuracy.

## Reading the report

For example:

```
ACTION REQUIRED  (the headline number: recall)
  recall 0.600   precision 1.000   f1 0.750   missed 4   false alarms 0
```

Precision 1.000 with recall 0.600 means the system is too conservative: it never
cries wolf, but it misses four things that needed you. For this product that is
the wrong trade — lower the accept threshold, add prototypes for the classes in
the confusion table, or mark the missed categories `high_stakes` so they always
escalate.

```
TRUST
  hallucination rate 0.000 (0/2 evidence spans not found in the email)
```

A non-zero rate means extracted values are being invented. Check whether the
schema is too large, whether the model is too small for the language, and
whether the evidence requirement is actually being enforced.
