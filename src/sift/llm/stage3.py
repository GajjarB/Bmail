"""Stage 3 — schema-constrained extraction with a local model.

The design decision that matters most in this file is the **evidence span**.
Every extracted date, amount or deadline must arrive with the sentence it came
from, and that sentence is then checked against the actual email text. It costs
a few tokens and buys three things:

* the UI can say *"interview 14 September — because this line says so"*;
* hallucination becomes visible instead of silent, and measurable as a metric;
* a field whose evidence does not appear in the email can be dropped
  automatically rather than trusted.

A valid JSON object is not a correct one. Constrained decoding guarantees the
shape; everything below re-checks the values.
"""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field

from sift.errors import LLMError
from sift.llm.client import OllamaClient, detect_tier
from sift.llm.prompts import (
    CLASSIFY_SYSTEM,
    EXTRACT_SYSTEM,
    classify_prompt,
    extract_prompt,
    repair_prompt,
)
from sift.llm.schema import ClassifyOut, ExtractOut, classify_schema, extract_schema
from sift.models import Message, Stage, Verdict
from sift.packs import Pack
from sift.text.dates import find_dates

EVIDENCE_SIMILARITY = 0.88


@dataclass(slots=True)
class Stage3Result:
    verdict: Verdict
    calls: int = 0
    tokens: int = 0
    repaired: bool = False
    problems: list[str] = field(default_factory=list)


def _normalise(text: str) -> str:
    return " ".join((text or "").split()).lower()


def evidence_supported(evidence: str, haystack: str) -> bool:
    """Is this sentence actually in the email?

    Exact containment first; then a fuzzy ratio, because models routinely fix
    the whitespace or drop a bracket when quoting. Anything below the threshold
    counts as unsupported.
    """
    needle, hay = _normalise(evidence), _normalise(haystack)
    if not needle:
        return False
    if needle in hay:
        return True
    window = len(needle)
    best = 0.0
    for start in range(0, max(1, len(hay) - window + 1), max(1, window // 3)):
        chunk = hay[start : start + window + 40]
        ratio = difflib.SequenceMatcher(None, needle, chunk).ratio()
        best = max(best, ratio)
        if best >= EVIDENCE_SIMILARITY:
            return True
    return False


class LLMStage:
    def __init__(
        self,
        pack: Pack,
        client: OllamaClient | None = None,
        *,
        model: str | None = None,
        num_ctx: int = 4096,
    ) -> None:
        self.pack = pack
        self.client = client or OllamaClient()
        self.model = model or detect_tier().model
        self.num_ctx = num_ctx

    # -- public ----------------------------------------------------------------- #

    def run(self, message: Message, verdict: Verdict) -> Stage3Result:
        result = Stage3Result(verdict=verdict)
        started = time.perf_counter()

        classification = self._classify(message, result)
        if classification is not None:
            verdict.category = classification.category
            verdict.action_required = classification.action_required
            verdict.urgency = classification.urgency
            verdict.next_action = classification.next_action
            verdict.summary = classification.summary or verdict.summary
            verdict.confidence = max(verdict.confidence, 0.8)

        extraction = self._extract(message, verdict.category, result)
        if extraction is not None:
            self._merge_extraction(message, verdict, extraction, result)

        verdict.stage = Stage.llm
        verdict.model = self.model
        verdict.latency_ms += int((time.perf_counter() - started) * 1000)
        verdict.reasons.append(f"local model {self.model}, {result.calls} call(s)")
        if result.problems:
            verdict.needs_review = True
            verdict.reasons.extend(result.problems)
        return result

    # -- calls ------------------------------------------------------------------- #

    def _classify(self, message: Message, result: Stage3Result) -> ClassifyOut | None:
        descriptions = {
            name: spec.description for name, spec in self.pack.categories.items()
        }
        schema = classify_schema(list(descriptions))
        try:
            raw, meta = self.client.chat_json(
                self.model,
                CLASSIFY_SYSTEM,
                classify_prompt(message, descriptions),
                schema,
                num_ctx=self.num_ctx,
            )
        except LLMError as exc:
            result.problems.append(f"classification failed: {exc}")
            return None
        result.calls += 1
        result.tokens += int(meta.get("eval_count", 0)) + int(meta.get("prompt_eval_count", 0))
        try:
            return ClassifyOut(**raw)
        except Exception as exc:
            result.problems.append(f"classification did not validate: {exc}")
            return None

    def _extract(self, message: Message, category: str, result: Stage3Result) -> ExtractOut | None:
        schema = extract_schema()
        prompt = extract_prompt(message, category)
        for attempt in (1, 2):
            try:
                raw, meta = self.client.chat_json(
                    self.model, EXTRACT_SYSTEM, prompt, schema, num_ctx=self.num_ctx
                )
            except LLMError as exc:
                result.problems.append(f"extraction failed: {exc}")
                return None
            result.calls += 1
            result.tokens += int(meta.get("eval_count", 0)) + int(meta.get("prompt_eval_count", 0))
            try:
                return ExtractOut(**raw)
            except Exception as exc:
                if attempt == 1:
                    # One repair attempt, then give up and flag for review — an
                    # endless retry loop is how a local pipeline melts a laptop.
                    result.repaired = True
                    prompt = repair_prompt(message, str(exc)[:300])
                    continue
                result.problems.append(f"extraction did not validate: {exc}")
        return None

    # -- validation --------------------------------------------------------------- #

    def _merge_extraction(
        self, message: Message, verdict: Verdict, extraction: ExtractOut, result: Stage3Result
    ) -> None:
        haystack = message.evidence_haystack()
        evidence_ok = (
            evidence_supported(extraction.evidence, haystack) if extraction.evidence else None
        )
        verdict.evidence = extraction.evidence
        verdict.evidence_ok = evidence_ok

        verdict.org = verdict.org or extraction.org
        verdict.role = verdict.role or extraction.role
        verdict.contact_person = verdict.contact_person or extraction.contact_person
        verdict.amount = verdict.amount or extraction.amount

        # A date the email does not contain is dropped, not shown. This is the
        # rule that keeps a wrong interview time out of the Brief.
        if extraction.event_datetime is not None:
            if self._date_supported(extraction.event_datetime.date().isoformat(), message):
                verdict.event_datetime = verdict.event_datetime or extraction.event_datetime
            else:
                result.problems.append("dropped event_datetime: not found in the email text")
        if extraction.deadline is not None:
            if self._date_supported(extraction.deadline.isoformat(), message):
                verdict.deadline = verdict.deadline or extraction.deadline
            else:
                result.problems.append("dropped deadline: not found in the email text")

        if evidence_ok is False:
            result.problems.append("evidence sentence does not appear in the email")

    @staticmethod
    def _date_supported(iso_value: str, message: Message) -> bool:
        """Cross-check a model date against the dates the deterministic parser sees."""
        text = message.evidence_haystack()
        if iso_value in text:
            return True
        parsed = {hit.value.isoformat() for hit in find_dates(text, reference=message.date.date())}
        return iso_value in parsed
