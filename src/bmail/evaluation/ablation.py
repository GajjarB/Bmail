"""Ablations: the study that turns a working app into a defensible result.

Five configurations, each isolating one decision:

1. **rules only** — how far do headers, the contact graph and template recipes
   get on their own? This number decides how much model you actually need, and
   it is usually higher than people expect.
2. **rules + classifier** — what does a cheap local embedding head add?
3. **rules + classifier + model** — the full hybrid.
4. **model only** — the naive baseline everyone builds first, for comparison on
   both accuracy *and* cost.
5. **language subsets** — the same pipeline scored separately on the German and
   English portions, because multilingual degradation is real and consistently
   under-reported.

Accuracy is only half of each row: latency and model calls belong beside it, or
the comparison is dishonest.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from bmail.classify.stage2 import Classifier
from bmail.evaluation.dataset import Dataset
from bmail.evaluation.harness import EvalReport, evaluate
from bmail.llm.stage3 import LLMStage
from bmail.packs import Pack
from bmail.pipeline import Pipeline


@dataclass
class AblationRow:
    name: str
    report: EvalReport

    def summary(self) -> dict[str, Any]:
        return {
            "config": self.name,
            "accuracy": round(self.report.category.accuracy, 4),
            "macro_f1": round(self.report.category.macro_f1, 4),
            "action_recall": round(self.report.action.recall, 4),
            "missed": self.report.action.false_negative,
            "p50_ms": self.report.latency_ms.get("p50", 0.0),
            "p95_ms": self.report.latency_ms.get("p95", 0.0),
            "llm_calls": self.report.stats.get("llm_calls", 0),
            "hallucination": round(self.report.hallucination.rate, 4),
        }


@dataclass
class AblationSuite:
    rows: list[AblationRow] = field(default_factory=list)

    def table(self) -> str:
        header = (
            f"{'configuration':28} {'acc':>6} {'macroF1':>8} {'act.recall':>11} "
            f"{'missed':>7} {'p50ms':>7} {'p95ms':>7} {'calls':>6}"
        )
        lines = [header, "-" * len(header)]
        for row in self.rows:
            summary = row.summary()
            lines.append(
                f"{summary['config'][:28]:28} {summary['accuracy']:6.3f} "
                f"{summary['macro_f1']:8.3f} {summary['action_recall']:11.3f} "
                f"{summary['missed']:7} {summary['p50_ms']:7.0f} {summary['p95_ms']:7.0f} "
                f"{summary['llm_calls']:6}"
            )
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {"rows": [row.summary() for row in self.rows]}


def run_ablations(
    conn: sqlite3.Connection,
    pack: Pack,
    dataset: Dataset,
    *,
    classifier: Classifier | None = None,
    llm: LLMStage | None = None,
    recipe_dir: str | None = None,
    by_language: bool = True,
) -> AblationSuite:
    suite = AblationSuite()

    configurations: list[tuple[str, Pipeline]] = [
        ("1. rules only", Pipeline(pack, classifier=None, llm=None, recipe_dir=recipe_dir,
                                   use_llm=False, distil=False)),
    ]
    if classifier is not None:
        configurations.append(
            ("2. rules + classifier",
             Pipeline(pack, classifier=classifier, llm=None, recipe_dir=recipe_dir,
                      use_llm=False, distil=False))
        )
    if classifier is not None and llm is not None:
        configurations.append(
            ("3. rules + classifier + LLM",
             Pipeline(pack, classifier=classifier, llm=llm, recipe_dir=recipe_dir,
                      use_llm=True, distil=False))
        )
        # The naive baseline: no rules, no classifier routing — every message to
        # the model. Included because it is what most projects build first.
        naive = Pipeline(pack, classifier=classifier, llm=llm, recipe_dir=recipe_dir,
                         use_llm=True, distil=False)
        naive.classifier = _AlwaysEscalate(classifier)
        configurations.append(("4. LLM on everything", naive))

    for name, pipeline in configurations:
        suite.rows.append(AblationRow(name, evaluate(conn, pack, pipeline, dataset, name=name)))

    if by_language and classifier is not None:
        full = Pipeline(pack, classifier=classifier, llm=llm, recipe_dir=recipe_dir,
                        use_llm=llm is not None, distil=False)
        for language in ("de", "en"):
            subset = Dataset([row for row in dataset.rows if (row.language or "") == language])
            if len(subset) >= 5:
                suite.rows.append(
                    AblationRow(
                        f"5. full · {language} only",
                        evaluate(conn, pack, full, subset, name=f"lang-{language}"),
                    )
                )
    return suite


class _AlwaysEscalate:
    """Wraps a classifier so every message is escalated — the naive baseline."""

    def __init__(self, inner: Classifier) -> None:
        self._inner = inner

    def run(self, message, verdict):  # type: ignore[no-untyped-def]
        result = self._inner.run(message, verdict)
        result.escalate = True
        result.reason = "baseline: no routing, every message goes to the model"
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
