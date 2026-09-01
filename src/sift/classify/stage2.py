"""Stage 2 — classify what Stage 1 could not, and decide what to escalate.

The routing rule is the point of this stage, and it is asymmetric on purpose:

* high confidence          -> accept the classifier's answer
* low confidence           -> escalate to the local model
* high-stakes category     -> escalate **regardless of confidence**

Missing an interview invitation is catastrophic. A false alarm costs three
seconds. The pack declares which categories are in the expensive-to-miss set;
this module just obeys it.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from sift.classify.features import Vectoriser, build_vectoriser, feature_text
from sift.classify.model import ClassifierBundle, Prediction
from sift.models import Lane, Message, Stage, Verdict
from sift.packs import Pack
from sift.store import repo


@dataclass(slots=True)
class Stage2Result:
    verdict: Verdict
    escalate: bool
    prediction: Prediction | None = None
    reason: str = ""


class Classifier:
    def __init__(
        self,
        pack: Pack,
        bundle: ClassifierBundle | None = None,
        vectoriser: Vectoriser | None = None,
        *,
        accept_threshold: float = 0.72,
    ) -> None:
        self.pack = pack
        self.bundle = bundle or ClassifierBundle(pack=pack.name)
        self.vectoriser = vectoriser or self._build_vectoriser(self.bundle.vectoriser)
        self.accept_threshold = accept_threshold
        self.high_stakes = pack.high_stakes()
        self.stale_bundle = self._discard_if_stale()
        if not self.bundle.prototype.prototypes:
            self.cold_start()

    @staticmethod
    def _build_vectoriser(name: str) -> Vectoriser:
        """Rebuild the saved vectoriser, falling back to the built-in one.

        A model saved on a machine with `sentence-transformers` installed must
        not make the CLI unusable on a machine without it; the bundle is then
        treated as stale and cold-started instead.
        """
        try:
            return build_vectoriser(name)
        except (ValueError, RuntimeError):
            return build_vectoriser("hashing")

    def _discard_if_stale(self) -> bool:
        """Drop a saved model that was trained with a different vectoriser.

        Vectors from two different backends are not comparable, and mixing them
        would produce confident nonsense rather than an error. Changing
        `classifier.vectoriser` in the config therefore costs a retrain, which is
        the honest trade.
        """
        bundle = self.bundle
        matches_name = not bundle.prototype.prototypes or bundle.vectoriser == self.vectoriser.name
        matches_dim = not self.vectoriser.dim or bundle.dim in (0, self.vectoriser.dim)
        if matches_name and matches_dim:
            return False
        self.bundle = ClassifierBundle(
            vectoriser=self.vectoriser.name, dim=self.vectoriser.dim, pack=self.pack.name
        )
        return True

    # -- cold start ----------------------------------------------------------- #

    def cold_start(self) -> None:
        """Seed prototypes from the phrases the pack declares. No labels needed."""
        seeds = {
            name: [*spec.prototypes, spec.description]
            for name, spec in self.pack.categories.items()
            if spec.prototypes or spec.description
        }
        self.bundle.prototype.seed(self.vectoriser, seeds)
        self.bundle.vectoriser = self.vectoriser.name
        self.bundle.dim = self.vectoriser.dim
        self.bundle.pack = self.pack.name

    # -- prediction ------------------------------------------------------------ #

    def predict(self, message: Message) -> Prediction | None:
        vector = self.vectoriser.encode([feature_text(message)])[0]
        return self._predict_vector(vector)

    def _predict_vector(self, vector: list[float]) -> Prediction | None:
        # The trained head wins when it exists; prototypes are the fallback and
        # the day-one path.
        return self.bundle.logistic.predict(vector) or self.bundle.prototype.predict(vector)

    def run(self, message: Message, verdict: Verdict) -> Stage2Result:
        started = time.perf_counter()
        prediction = self.predict(message)
        if prediction is None:
            verdict.reasons.append("classifier unavailable")
            return Stage2Result(verdict, escalate=True, reason="no classifier")

        verdict.category = prediction.category
        verdict.confidence = round(prediction.probability, 3)
        verdict.stage = Stage.classifier
        verdict.lane = Lane.candidate
        verdict.model = f"{self.bundle.vectoriser}+{prediction.head}"
        verdict.reasons.append(
            f"{prediction.head} head: {prediction.category} p={prediction.probability:.2f}"
        )
        spec = self.pack.spec(prediction.category)
        verdict.action_required = spec.action_required
        verdict.urgency = spec.urgency
        verdict.next_action = spec.next_action
        verdict.latency_ms += int((time.perf_counter() - started) * 1000)

        if prediction.category in self.high_stakes:
            return Stage2Result(
                verdict, escalate=True, prediction=prediction,
                reason=f"'{prediction.category}' is expensive to get wrong",
            )
        if prediction.probability < self.accept_threshold:
            return Stage2Result(
                verdict, escalate=True, prediction=prediction,
                reason=f"confidence {prediction.probability:.2f} below {self.accept_threshold:.2f}",
            )
        return Stage2Result(verdict, escalate=False, prediction=prediction)

    # -- learning -------------------------------------------------------------- #

    def learn(self, message: Message, category: str) -> None:
        """Fold one confirmed label into the prototypes immediately."""
        vector = self.vectoriser.encode([feature_text(message)])[0]
        self.bundle.prototype.update(category, vector)

    def train(
        self, conn: sqlite3.Connection, *, holdout: float = 0.2, min_rows: int = 40
    ) -> dict[str, object]:
        """Train the logistic head on stored labels and calibrate it.

        Labels come from two places: your corrections in the UI, and — after the
        first backfill — the local model's own output, distilled into the cheap
        head so the expensive stage runs less often over time.
        """
        rows = repo.training_rows(conn, self.pack.name)
        if len(rows) < min_rows:
            return {
                "trained": False,
                "rows": len(rows),
                "reason": f"need at least {min_rows} labelled rows, have {len(rows)}",
            }
        texts = [feature_text(message) for message, _ in rows]
        labels = [label for _, label in rows]
        vectors = self.vectoriser.encode(texts)

        split = max(1, int(len(vectors) * (1 - holdout)))
        try:
            self.bundle.logistic.fit(vectors[:split], labels[:split])
        except RuntimeError as exc:
            return {"trained": False, "rows": len(rows), "reason": str(exc)}
        temperature = self.bundle.logistic.calibrate(vectors[split:], labels[split:])

        correct = sum(
            1
            for vector, label in zip(vectors[split:], labels[split:], strict=True)
            if (prediction := self.bundle.logistic.predict(vector)) and prediction.category == label
        )
        held = max(1, len(vectors) - split)
        return {
            "trained": True,
            "rows": len(rows),
            "classes": len(self.bundle.logistic.classes),
            "holdout_accuracy": round(correct / held, 3),
            "temperature": temperature,
        }

    def save(self, path: str) -> None:
        self.bundle.save(path)
