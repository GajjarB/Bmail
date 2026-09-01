"""Run an evaluation over a labelled split and produce one report.

The harness re-triages messages through a pipeline rather than reading stored
verdicts, so a configuration can be evaluated without disturbing the live store.
Everything it needs is a connection, a pack, a pipeline and a dataset.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sift.evaluation.dataset import Dataset
from sift.evaluation.metrics import (
    BinaryMetrics,
    ClassificationReport,
    FieldReport,
    HallucinationReport,
    binary_metrics,
    classification_report,
    field_report,
    latency_summary,
    precision_at_k,
)
from sift.llm.stage3 import evidence_supported
from sift.models import Message, Verdict
from sift.packs import Pack
from sift.pipeline import Pipeline, TriageStats
from sift.store import repo


@dataclass
class EvalReport:
    name: str = "run"
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evaluated: int = 0
    skipped: int = 0
    category: ClassificationReport = field(default_factory=ClassificationReport)
    action: BinaryMetrics = field(default_factory=BinaryMetrics)
    fields: dict[str, FieldReport] = field(default_factory=dict)
    hallucination: HallucinationReport = field(default_factory=HallucinationReport)
    latency_ms: dict[str, float] = field(default_factory=dict)
    precision_at_5: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "generated_at": self.generated_at.isoformat(),
            "evaluated": self.evaluated,
            "skipped": self.skipped,
            "category": self.category.as_dict(),
            "action_required": self.action.as_dict(),
            "fields": {name: report.as_dict() for name, report in self.fields.items()},
            "hallucination": self.hallucination.as_dict(),
            "latency_ms": self.latency_ms,
            "precision_at_5": round(self.precision_at_5, 4),
            "pipeline": self.stats,
        }

    def render(self) -> str:
        lines = [
            f"Evaluation · {self.name} · {self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
            f"{self.evaluated} messages evaluated ({self.skipped} skipped: not ingested)",
            "",
            "CATEGORY",
            self.category.table(),
            "",
            "ACTION REQUIRED  (the headline number: recall)",
        ]
        action = self.action.as_dict()
        lines.append(
            f"  recall {action['recall']:.3f}   precision {action['precision']:.3f}   "
            f"f1 {action['f1']:.3f}   missed {action['missed']}   "
            f"false alarms {action['false_alarms']}"
        )
        if self.fields:
            lines += ["", "FIELD EXTRACTION"]
            for name, report in self.fields.items():
                lines.append(
                    f"  {name:16} accuracy {report.accuracy:.3f} over {report.compared:4} "
                    f"(missing {report.missing}, spurious {report.spurious})"
                )
        lines += [
            "",
            "TRUST",
            f"  hallucination rate {self.hallucination.rate:.3f} "
            f"({self.hallucination.unsupported}/{self.hallucination.checked} evidence spans "
            f"not found in the email; {self.hallucination.no_evidence} offered none)",
            f"  precision@5 on the Brief {self.precision_at_5:.3f}",
            "",
            "COST",
            f"  latency per message: p50 {self.latency_ms.get('p50', 0):.0f} ms, "
            f"p95 {self.latency_ms.get('p95', 0):.0f} ms, max {self.latency_ms.get('max', 0):.0f} ms",
        ]
        if self.stats:
            by_stage = self.stats.get("by_stage", {})
            lines.append(f"  resolved by stage: {by_stage}")
            lines.append(
                f"  model calls {self.stats.get('llm_calls', 0)}, "
                f"tokens {self.stats.get('llm_tokens', 0)}"
            )
        return "\n".join(lines) + "\n"

    def save(self, path: str | Path) -> Path:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path


def evaluate(
    conn: sqlite3.Connection,
    pack: Pack,
    pipeline: Pipeline,
    dataset: Dataset,
    *,
    name: str = "run",
    persist: bool = False,
) -> EvalReport:
    """Triage the labelled messages and score the result.

    With `persist=False` (the default) verdicts are computed in a savepoint that
    is rolled back, so evaluating never contaminates the live store — which is
    the property that makes the locked test split meaningful.
    """
    report = EvalReport(name=name)
    labels = dataset.by_id()
    stats = TriageStats()

    messages: list[Message] = []
    for message_id in labels:
        message = repo.get_message(conn, message_id)
        if message is None:
            report.skipped += 1
            continue
        messages.append(message)
    messages.sort(key=lambda m: m.date)

    verdicts: dict[str, Verdict] = {}
    latencies: list[int] = []
    # Scoring must never read back a human correction: the pipeline honours a
    # manual verdict in normal use, and doing so here would measure the label
    # rather than the model. Turned off for the duration, restored after.
    honours_manual = getattr(pipeline, "respect_manual", False)
    pipeline.respect_manual = False
    conn.execute("SAVEPOINT sift_eval")
    try:
        for message in messages:
            started = time.perf_counter()
            verdict = pipeline.triage_one(conn, message, stats)
            latencies.append(int((time.perf_counter() - started) * 1000))
            verdicts[message.message_id] = verdict
    finally:
        pipeline.respect_manual = honours_manual
        if persist:
            conn.execute("RELEASE SAVEPOINT sift_eval")
        else:
            conn.execute("ROLLBACK TO SAVEPOINT sift_eval")
            conn.execute("RELEASE SAVEPOINT sift_eval")

    category_pairs: list[tuple[str, str]] = []
    action_pairs: list[tuple[bool, bool]] = []
    field_pairs: dict[str, list[tuple[Any, Any]]] = {
        "org": [], "role": [], "deadline": [], "event_datetime": []
    }

    for message in messages:
        gold = labels[message.message_id]
        verdict = verdicts[message.message_id]
        report.evaluated += 1
        if gold.category:
            category_pairs.append((gold.category, verdict.category))
        if gold.action_required is not None:
            action_pairs.append((bool(gold.action_required), bool(verdict.action_required)))
        if gold.org is not None:
            field_pairs["org"].append((gold.org, verdict.org))
        if gold.role is not None:
            field_pairs["role"].append((gold.role, verdict.role))
        if gold.deadline is not None:
            field_pairs["deadline"].append((gold.deadline, verdict.deadline))
        if gold.event_datetime is not None:
            field_pairs["event_datetime"].append((gold.event_datetime, verdict.event_datetime))

        if verdict.evidence:
            report.hallucination.checked += 1
            if not evidence_supported(verdict.evidence, message.evidence_haystack()):
                report.hallucination.unsupported += 1
        elif verdict.deadline or verdict.event_datetime:
            report.hallucination.no_evidence += 1

    report.category = classification_report(category_pairs)
    report.action = binary_metrics(action_pairs)
    for name_, pairs in field_pairs.items():
        if pairs:
            report.fields[name_] = field_report(
                name_, pairs, exact=name_ in ("deadline", "event_datetime")
            )
    report.latency_ms = latency_summary(latencies)
    report.stats = stats.as_dict()

    ranked = sorted(
        (verdicts[m.message_id] for m in messages),
        key=lambda v: (v.urgency.value != "today", v.urgency.value != "this_week",
                       not v.action_required, -v.confidence),
    )
    relevance = [
        bool(labels[v.message_id].action_required)
        for v in ranked
        if labels[v.message_id].action_required is not None
    ]
    report.precision_at_5 = precision_at_k(relevance, 5)
    return report
