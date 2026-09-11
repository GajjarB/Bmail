"""Metrics, chosen for this task rather than copied from a textbook.

Four of them earn their place:

* **Recall on `action_required`** is the headline number, not macro-F1. Missing
  an interview invitation is catastrophic; a false alarm costs three seconds, so
  the operating point matters more than the area under any curve.
* **Precision@k on the Brief** is the product metric. If the top five items each
  morning are not right, nothing else rescues it.
* **Field accuracy** is split by field type: exact match on dates (with the
  timezone), fuzzy match on organisation names, because "Acme GmbH" and "Acme"
  are the same employer and pretending otherwise measures the wrong thing.
* **Hallucination rate** is the fraction of extracted values whose evidence span
  does not contain them. It is cheap to compute automatically and it is the
  number that decides whether the output can be trusted without checking.
"""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(slots=True)
class ClassMetrics:
    label: str
    support: int = 0
    predicted: int = 0
    true_positive: int = 0

    @property
    def precision(self) -> float:
        return self.true_positive / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positive / self.support if self.support else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


@dataclass(slots=True)
class ClassificationReport:
    per_class: dict[str, ClassMetrics] = field(default_factory=dict)
    confusion: Counter = field(default_factory=Counter)
    total: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def macro_f1(self) -> float:
        scored = [m for m in self.per_class.values() if m.support]
        return sum(m.f1 for m in scored) / len(scored) if scored else 0.0

    @property
    def weighted_f1(self) -> float:
        scored = [m for m in self.per_class.values() if m.support]
        support = sum(m.support for m in scored)
        return sum(m.f1 * m.support for m in scored) / support if support else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accuracy": round(self.accuracy, 4),
            "macro_f1": round(self.macro_f1, 4),
            "weighted_f1": round(self.weighted_f1, 4),
            "per_class": {
                label: {
                    "support": metric.support,
                    "precision": round(metric.precision, 4),
                    "recall": round(metric.recall, 4),
                    "f1": round(metric.f1, 4),
                }
                for label, metric in sorted(self.per_class.items())
            },
        }

    def table(self, top_confusions: int = 8) -> str:
        lines = [
            f"{'class':24} {'support':>7} {'prec':>7} {'recall':>7} {'f1':>7}",
            "-" * 58,
        ]
        for label, metric in sorted(self.per_class.items(), key=lambda kv: -kv[1].support):
            if not metric.support and not metric.predicted:
                continue
            lines.append(
                f"{label[:24]:24} {metric.support:7} {metric.precision:7.3f} "
                f"{metric.recall:7.3f} {metric.f1:7.3f}"
            )
        lines += [
            "-" * 58,
            f"{'accuracy':24} {self.total:7} {'':7} {'':7} {self.accuracy:7.3f}",
            f"{'macro f1':24} {'':7} {'':7} {'':7} {self.macro_f1:7.3f}",
        ]
        mistakes = [(pair, count) for pair, count in self.confusion.most_common() if pair[0] != pair[1]]
        if mistakes:
            lines += ["", f"top confusions (gold -> predicted), {len(mistakes)} distinct:"]
            for (gold, predicted), count in mistakes[:top_confusions]:
                lines.append(f"  {gold:22} -> {predicted:22} {count:4}")
        return "\n".join(lines)


def classification_report(pairs: list[tuple[str, str]]) -> ClassificationReport:
    """`pairs` is a list of (gold, predicted)."""
    report = ClassificationReport()
    metrics: dict[str, ClassMetrics] = defaultdict(lambda: ClassMetrics(label=""))
    for gold, predicted in pairs:
        report.total += 1
        report.confusion[(gold, predicted)] += 1
        for label in (gold, predicted):
            if not metrics[label].label:
                metrics[label].label = label
        metrics[gold].support += 1
        metrics[predicted].predicted += 1
        if gold == predicted:
            metrics[gold].true_positive += 1
            report.correct += 1
    report.per_class = dict(metrics)
    return report


@dataclass(slots=True)
class BinaryMetrics:
    """For `action_required`, where recall is the number that matters."""

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "missed": self.false_negative,
            "false_alarms": self.false_positive,
        }


def binary_metrics(pairs: list[tuple[bool, bool]]) -> BinaryMetrics:
    metrics = BinaryMetrics()
    for gold, predicted in pairs:
        if gold and predicted:
            metrics.true_positive += 1
        elif gold and not predicted:
            metrics.false_negative += 1
        elif predicted:
            metrics.false_positive += 1
        else:
            metrics.true_negative += 1
    return metrics


def precision_at_k(ranked_relevance: list[bool], k: int = 5) -> float:
    """Fraction of the top-k Brief items that genuinely needed the user."""
    top = ranked_relevance[:k]
    return sum(top) / len(top) if top else 0.0


def fuzzy_equal(a: str | None, b: str | None, *, threshold: float = 0.85) -> bool:
    if not a and not b:
        return True
    if not a or not b:
        return False
    left, right = " ".join(a.lower().split()), " ".join(b.lower().split())
    if left == right or left in right or right in left:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= threshold


def exact_date_equal(a: Any, b: Any) -> bool:
    """Dates must match exactly — including the time and zone when both carry one."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, str):
        a = _parse(a)
    if isinstance(b, str):
        b = _parse(b)
    if a is None or b is None:
        return False
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a.utctimetuple()[:6] == b.utctimetuple()[:6]
    left = a.date() if isinstance(a, datetime) else a
    right = b.date() if isinstance(b, datetime) else b
    return left == right


def _parse(value: str) -> datetime | date | None:
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass(slots=True)
class FieldReport:
    field: str
    compared: int = 0
    correct: int = 0
    missing: int = 0        # gold has a value, the system does not
    spurious: int = 0       # the system has a value, gold does not

    @property
    def accuracy(self) -> float:
        return self.correct / self.compared if self.compared else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "accuracy": round(self.accuracy, 4),
            "missing": self.missing,
            "spurious": self.spurious,
        }


def field_report(field_name: str, pairs: list[tuple[Any, Any]], *, exact: bool) -> FieldReport:
    report = FieldReport(field=field_name)
    for gold, predicted in pairs:
        report.compared += 1
        same = exact_date_equal(gold, predicted) if exact else fuzzy_equal(gold, predicted)
        if same:
            report.correct += 1
        elif gold and not predicted:
            report.missing += 1
        elif predicted and not gold:
            report.spurious += 1
    return report


@dataclass(slots=True)
class HallucinationReport:
    checked: int = 0
    unsupported: int = 0
    no_evidence: int = 0

    @property
    def rate(self) -> float:
        return self.unsupported / self.checked if self.checked else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "unsupported": self.unsupported,
            "no_evidence_offered": self.no_evidence,
            "rate": round(self.rate, 4),
        }


def latency_summary(samples: list[int]) -> dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "max": 0.0}
    ordered = sorted(samples)
    return {
        "p50": float(ordered[len(ordered) // 2]),
        "p95": float(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]),
        "mean": round(sum(ordered) / len(ordered), 1),
        "max": float(ordered[-1]),
    }
