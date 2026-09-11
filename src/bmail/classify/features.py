"""Vectorisers.

Three backends, chosen by what is installed and what the hardware can carry:

* `hashing` — pure Python, no dependencies, no download. Character n-grams
  survive German compounding and typos better than word features, which matters
  for a bilingual corpus. This is the default so that a fresh checkout works.
* `sentence-transformers` — a real multilingual embedding model, run locally.
* `ollama` — embeddings from a model already served on 127.0.0.1.

They are interchangeable: the classifier only sees a list of floats.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from bmail.models import Message
from bmail.text.clean import normalise_whitespace

_WORD = re.compile(r"[\w][\w'’-]*", re.UNICODE)

FEATURE_CHARS = 600  # subject + first ~600 chars, per the design note


def feature_text(message: Message) -> str:
    """The text a classifier sees. Deliberately short: the decisive signal in
    mail is in the subject and opening lines, and truncating keeps it fast."""
    body = message.body_text[:FEATURE_CHARS]
    sender = f"{message.from_.name} {message.from_.address}".strip()
    return normalise_whitespace(f"{message.subject}\n{sender}\n{body}")


class Vectoriser(Protocol):
    dim: int
    name: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def _l2(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class HashingVectoriser:
    """Hashed word unigrams plus character 3–5 grams, L2-normalised.

    Not as strong as a trained embedding model, but it needs no download, runs
    in microseconds, and gives the nearest-prototype cold start something real
    to work with on day one.
    """

    name = "hashing"

    def __init__(self, dim: int = 1024, char_ngrams: tuple[int, ...] = (3, 4, 5)) -> None:
        self.dim = dim
        self.char_ngrams = char_ngrams

    def _bucket(self, token: str) -> int:
        return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big")

    def encode_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        lowered = f" {text.lower()} "
        for word in _WORD.findall(lowered):
            index = self._bucket(f"w:{word}")
            vector[index % self.dim] += 1.0 if index >> 31 & 1 else -1.0
        for size in self.char_ngrams:
            for start in range(max(0, len(lowered) - size + 1)):
                gram = lowered[start : start + size]
                if gram.isspace():
                    continue
                index = self._bucket(f"c:{gram}")
                vector[index % self.dim] += 0.5 if index >> 31 & 1 else -0.5
        return _l2(vector)

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self.encode_one(text) for text in texts]


class SentenceTransformerVectoriser:
    """A local multilingual embedding model. Nothing is sent anywhere."""

    def __init__(self, model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "install the optional extra: pip install 'bmail[embeddings]'"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, vector)) for vector in vectors]


class OllamaVectoriser:
    """Embeddings from a model already running on the local Ollama daemon."""

    def __init__(self, model: str = "nomic-embed-text", host: str = "http://127.0.0.1:11434") -> None:
        from bmail.llm.client import OllamaClient

        self._client = OllamaClient(host=host)
        self._model = model
        self.name = f"ollama:{model}"
        self.dim = 0  # discovered on first call

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = [self._client.embed(self._model, text) for text in texts]
        if vectors and not self.dim:
            self.dim = len(vectors[0])
        return [_l2(vector) for vector in vectors]


def build_vectoriser(kind: str = "hashing", **kwargs: object) -> Vectoriser:
    """Build a vectoriser from a config value or from a saved bundle's name.

    Saved bundles record the *resolved* name (`st:<model>`, `ollama:<model>`),
    so both forms are accepted here — otherwise a bundle could be written but
    never loaded back.
    """
    kind = (kind or "hashing").lower()
    if ":" in kind:
        prefix, _, model = kind.partition(":")
        if prefix in ("st", "sentence-transformers"):
            return SentenceTransformerVectoriser(model)
        if prefix == "ollama":
            return OllamaVectoriser(model=model, host=str(kwargs.get("host", "http://127.0.0.1:11434")))
        raise ValueError(f"unknown vectoriser '{kind}'")
    if kind == "hashing":
        return HashingVectoriser(dim=int(kwargs.get("dim", 1024)))  # type: ignore[arg-type]
    if kind in ("sentence-transformers", "st"):
        return SentenceTransformerVectoriser(
            str(kwargs.get("model", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"))
        )
    if kind == "ollama":
        return OllamaVectoriser(
            model=str(kwargs.get("model", "nomic-embed-text")),
            host=str(kwargs.get("host", "http://127.0.0.1:11434")),
        )
    raise ValueError(f"unknown vectoriser '{kind}'")


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
