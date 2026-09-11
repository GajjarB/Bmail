"""The Stage 2 classifier: cold start, personalisation, calibration, routing.

Two heads, both operating on the same vectors:

* `PrototypeHead` — nearest label prototype, built from the seed phrases the
  pack declares. Works with zero labelled data, which is the state you are in
  on day one.
* `LogisticHead` — trained on confirmed labels once you have them. Needs numpy;
  falls back to prototypes when it is not installed or not yet trained.

Both produce calibrated probabilities, because the routing decision — accept, or
escalate to the language model — is only as good as the confidence estimate
behind it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from bmail.classify.features import Vectoriser, cosine

try:  # pragma: no cover - optional dependency
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


@dataclass(slots=True)
class Prediction:
    category: str
    probability: float
    scores: dict[str, float] = field(default_factory=dict)
    head: str = "prototype"

    @property
    def margin(self) -> float:
        ordered = sorted(self.scores.values(), reverse=True)
        return ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0] if ordered else 0.0


def _softmax(scores: dict[str, float], temperature: float) -> dict[str, float]:
    if not scores:
        return {}
    temperature = max(temperature, 1e-3)
    top = max(scores.values())
    exponentials = {name: math.exp((value - top) / temperature) for name, value in scores.items()}
    total = sum(exponentials.values()) or 1.0
    return {name: value / total for name, value in exponentials.items()}


class PrototypeHead:
    """Nearest class prototype, seeded from pack descriptions.

    Every user correction is folded into the class centroid, so the head becomes
    personal without a training run — which is what makes day-one usable and
    week-two good.
    """

    name = "prototype"

    def __init__(self, temperature: float = 0.08) -> None:
        self.prototypes: dict[str, list[float]] = {}
        self.counts: dict[str, int] = {}
        self.temperature = temperature

    def seed(self, vectoriser: Vectoriser, seeds: dict[str, list[str]]) -> None:
        for category, phrases in seeds.items():
            phrases = [phrase for phrase in phrases if phrase.strip()]
            if not phrases:
                continue
            vectors = vectoriser.encode(phrases)
            self.prototypes[category] = _mean(vectors)
            self.counts[category] = len(vectors)

    def update(self, category: str, vector: list[float]) -> None:
        """Running-mean update: one correction, one cheap centroid move."""
        current = self.prototypes.get(category)
        count = self.counts.get(category, 0)
        if current is None:
            self.prototypes[category] = list(vector)
            self.counts[category] = 1
            return
        weight = 1.0 / (count + 1)
        self.prototypes[category] = [
            old * (1 - weight) + new * weight for old, new in zip(current, vector, strict=True)
        ]
        self.counts[category] = count + 1

    def predict(self, vector: list[float]) -> Prediction | None:
        if not self.prototypes:
            return None
        scores = {name: cosine(vector, proto) for name, proto in self.prototypes.items()}
        probabilities = _softmax(scores, self.temperature)
        best = max(probabilities, key=lambda name: probabilities[name])
        return Prediction(best, probabilities[best], scores, self.name)

    def to_dict(self) -> dict:
        return {
            "prototypes": self.prototypes,
            "counts": self.counts,
            "temperature": self.temperature,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PrototypeHead:
        head = cls(temperature=float(data.get("temperature", 0.08)))
        head.prototypes = {k: list(map(float, v)) for k, v in data.get("prototypes", {}).items()}
        head.counts = {k: int(v) for k, v in data.get("counts", {}).items()}
        return head


class LogisticHead:
    """Multinomial logistic regression with temperature-scaled probabilities.

    scikit-learn is used when present (better optimiser, and it is the obvious
    thing to reach for in a study); otherwise a plain numpy gradient descent
    does the job for the few thousand rows this ever sees.
    """

    name = "logistic"

    def __init__(self, temperature: float = 1.0) -> None:
        self.classes: list[str] = []
        self.weights: list[list[float]] = []
        self.bias: list[float] = []
        self.temperature = temperature
        self.trained = False

    def fit(
        self,
        vectors: list[list[float]],
        labels: list[str],
        *,
        epochs: int = 250,
        learning_rate: float = 0.5,
        l2: float = 1e-4,
    ) -> None:
        if _np is None:
            raise RuntimeError("training the logistic head needs numpy: pip install 'bmail[ml]'")
        self.classes = sorted(set(labels))
        if len(self.classes) < 2 or len(vectors) < len(self.classes) * 2:
            raise RuntimeError(
                f"not enough labelled data: {len(vectors)} rows over {len(self.classes)} classes"
            )
        index = {name: position for position, name in enumerate(self.classes)}
        X = _np.asarray(vectors, dtype=_np.float64)
        y = _np.asarray([index[label] for label in labels], dtype=_np.int64)

        try:  # pragma: no cover - depends on optional install
            from sklearn.linear_model import LogisticRegression

            model = LogisticRegression(max_iter=1000, C=1.0 / max(l2, 1e-9), multi_class="auto")
            model.fit(X, y)
            coefficients = _np.atleast_2d(model.coef_)
            intercepts = _np.atleast_1d(model.intercept_)
            if coefficients.shape[0] == 1 and len(self.classes) == 2:
                coefficients = _np.vstack([-coefficients[0], coefficients[0]])
                intercepts = _np.array([-intercepts[0], intercepts[0]])
        except ImportError:
            coefficients, intercepts = _gradient_descent(X, y, len(self.classes), epochs,
                                                         learning_rate, l2)

        self.weights = coefficients.tolist()
        self.bias = intercepts.tolist()
        self.trained = True

    def predict(self, vector: list[float]) -> Prediction | None:
        if not self.trained:
            return None
        logits = {
            name: sum(w * v for w, v in zip(self.weights[position], vector, strict=True)) + self.bias[position]
            for position, name in enumerate(self.classes)
        }
        probabilities = _softmax(logits, self.temperature)
        best = max(probabilities, key=lambda name: probabilities[name])
        return Prediction(best, probabilities[best], logits, self.name)

    def calibrate(self, vectors: list[list[float]], labels: list[str]) -> float:
        """Fit the softmax temperature on held-out data.

        Uncalibrated confidences are the reason confidence routing quietly fails:
        a head that says 0.97 for everything cannot decide what to escalate.
        """
        if not self.trained or not vectors:
            return self.temperature
        best_temperature, best_loss = self.temperature, float("inf")
        for candidate in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0):
            self.temperature = candidate
            loss = 0.0
            for vector, label in zip(vectors, labels, strict=True):
                prediction = self.predict(vector)
                if prediction is None:
                    continue
                probability = _softmax(prediction.scores, candidate).get(label, 1e-9)
                loss -= math.log(max(probability, 1e-9))
            if loss < best_loss:
                best_temperature, best_loss = candidate, loss
        self.temperature = best_temperature
        return best_temperature

    def to_dict(self) -> dict:
        return {
            "classes": self.classes,
            "weights": self.weights,
            "bias": self.bias,
            "temperature": self.temperature,
            "trained": self.trained,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LogisticHead:
        head = cls(temperature=float(data.get("temperature", 1.0)))
        head.classes = list(data.get("classes", []))
        head.weights = [list(map(float, row)) for row in data.get("weights", [])]
        head.bias = list(map(float, data.get("bias", [])))
        head.trained = bool(data.get("trained", False))
        return head


def _gradient_descent(X, y, n_classes: int, epochs: int, learning_rate: float, l2: float):  # type: ignore[no-untyped-def]
    """Plain multinomial logistic regression, for installs without scikit-learn."""
    samples, features = X.shape
    W = _np.zeros((n_classes, features))
    b = _np.zeros(n_classes)
    Y = _np.zeros((samples, n_classes))
    Y[_np.arange(samples), y] = 1.0
    for _ in range(epochs):
        logits = X @ W.T + b
        logits -= logits.max(axis=1, keepdims=True)
        exponentials = _np.exp(logits)
        probabilities = exponentials / exponentials.sum(axis=1, keepdims=True)
        error = probabilities - Y
        W -= learning_rate * ((error.T @ X) / samples + l2 * W)
        b -= learning_rate * error.mean(axis=0)
    return W, b


def _mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    length = len(vectors[0])
    totals = [0.0] * length
    for vector in vectors:
        for position, value in enumerate(vector):
            totals[position] += value
    norm = math.sqrt(sum(value * value for value in totals)) or 1.0
    return [value / norm for value in totals]


@dataclass
class ClassifierBundle:
    """What gets persisted next to the store: both heads plus the vectoriser name."""

    vectoriser: str = "hashing"
    dim: int = 1024
    prototype: PrototypeHead = field(default_factory=PrototypeHead)
    logistic: LogisticHead = field(default_factory=LogisticHead)
    pack: str = ""

    def save(self, path: str | Path) -> None:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "vectoriser": self.vectoriser,
                    "dim": self.dim,
                    "pack": self.pack,
                    "prototype": self.prototype.to_dict(),
                    "logistic": self.logistic.to_dict(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> ClassifierBundle | None:
        path = Path(path).expanduser()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return cls(
            vectoriser=data.get("vectoriser", "hashing"),
            dim=int(data.get("dim", 1024)),
            pack=data.get("pack", ""),
            prototype=PrototypeHead.from_dict(data.get("prototype", {})),
            logistic=LogisticHead.from_dict(data.get("logistic", {})),
        )
