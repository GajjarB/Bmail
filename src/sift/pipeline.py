"""The triage pipeline: Stage 1 -> 2 -> 3 -> 4, with honest accounting.

Every message enters at Stage 1 and leaves as soon as something is confident
enough to settle it. The `TriageStats` returned at the end reports where each
message actually stopped, which is the number that tells you whether the
expensive stage is earning its keep.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sift.classify.stage2 import Classifier
from sift.entities.stage4 import CaseResolver, refresh_derived
from sift.errors import LLMError
from sift.llm.stage3 import LLMStage
from sift.models import Message, Stage, Verdict
from sift.packs import Pack
from sift.rules.engine import RulesEngine
from sift.store import repo

ProgressHook = Callable[[int, int, Message], None]


@dataclass(slots=True)
class TriageStats:
    processed: int = 0
    by_stage: Counter[str] = field(default_factory=Counter)
    by_category: Counter[str] = field(default_factory=Counter)
    by_lane: Counter[str] = field(default_factory=Counter)
    llm_calls: int = 0
    llm_tokens: int = 0
    escalated: int = 0
    needs_review: int = 0
    cases_created: int = 0
    cases_transitioned: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "by_stage": dict(self.by_stage),
            "by_category": dict(self.by_category),
            "by_lane": dict(self.by_lane),
            "llm_calls": self.llm_calls,
            "llm_tokens": self.llm_tokens,
            "escalated": self.escalated,
            "needs_review": self.needs_review,
            "cases_created": self.cases_created,
            "cases_transitioned": self.cases_transitioned,
            "errors": self.errors[:20],
        }


class Pipeline:
    def __init__(
        self,
        pack: Pack,
        *,
        classifier: Classifier | None = None,
        llm: LLMStage | None = None,
        recipe_dir: str | None = None,
        use_llm: bool = True,
        max_llm_calls: int = 500,
        distil: bool = True,
        respect_manual: bool = True,
    ) -> None:
        self.pack = pack
        self.rules = RulesEngine(pack, recipe_dir=recipe_dir)
        self.classifier = classifier
        self.llm = llm if use_llm else None
        self.resolver = CaseResolver(pack)
        self.max_llm_calls = max_llm_calls
        self.distil = distil
        self.respect_manual = respect_manual

    # -- one message ------------------------------------------------------------ #

    def triage_one(
        self, conn: sqlite3.Connection, message: Message, stats: TriageStats
    ) -> Verdict:
        if self.respect_manual:
            # A human correction outranks anything the pipeline would decide.
            # Re-triaging must not quietly undo the answer someone typed in — and
            # the case machinery below has to see the human's category too, so
            # this returns early rather than merely skipping the write.
            existing = repo.get_verdict(conn, message.message_id, self.pack.name)
            if existing is not None and existing.stage is Stage.manual:
                self.rules.remember(message, existing)
                self._settle(conn, message, existing, stats)
                return existing

        stage1 = self.rules.run(conn, message)
        verdict = stage1.verdict

        if not stage1.resolved and self.classifier is not None:
            stage2 = self.classifier.run(message, verdict)
            verdict = stage2.verdict
            if stage2.escalate:
                stats.escalated += 1
                verdict.reasons.append(f"escalated: {stage2.reason}")
                if self.llm is not None and stats.llm_calls < self.max_llm_calls:
                    try:
                        stage3 = self.llm.run(message, verdict)
                        stats.llm_calls += stage3.calls
                        stats.llm_tokens += stage3.tokens
                        verdict = stage3.verdict
                    except LLMError as exc:
                        # A missing model must degrade the run, never end it.
                        verdict.needs_review = True
                        verdict.reasons.append(f"local model unavailable: {exc}")
                        stats.errors.append(str(exc))
                else:
                    verdict.needs_review = True
            # Feed the settled answer forward so later duplicates inherit it.
            self.rules.remember(message, verdict)
            if self.distil and verdict.stage is Stage.llm and not verdict.needs_review:
                # Distillation: the expensive stage teaches the cheap one, so the
                # same mail shape costs nothing next month.
                repo.add_training_row(
                    conn, message.message_id, self.pack.name, verdict.category,
                    verdict.action_required, source="distilled",
                )
        elif not stage1.resolved:
            verdict.needs_review = True
            verdict.reasons.append("no classifier configured")

        repo.save_verdict(conn, verdict)
        self._settle(conn, message, verdict, stats)
        return verdict

    def _settle(
        self,
        conn: sqlite3.Connection,
        message: Message,
        verdict: Verdict,
        stats: TriageStats,
    ) -> None:
        """Run entity resolution for a finished verdict and count it."""
        stage4 = self.resolver.resolve(conn, message, verdict)
        if stage4.case is not None:
            stats.cases_created += int(stage4.created)
            stats.cases_transitioned += int(stage4.transitioned and not stage4.created)

        stats.processed += 1
        stats.by_stage[verdict.stage.value] += 1
        stats.by_category[verdict.category] += 1
        stats.by_lane[verdict.lane.value] += 1
        stats.needs_review += int(verdict.needs_review)

    # -- batch -------------------------------------------------------------------- #

    def triage(
        self,
        conn: sqlite3.Connection,
        messages: list[Message],
        *,
        progress: ProgressHook | None = None,
        commit_every: int = 25,
    ) -> TriageStats:
        stats = TriageStats()
        # Oldest first: a case's state machine only makes sense in arrival order.
        ordered = sorted(messages, key=lambda m: m.date)
        for index, message in enumerate(ordered, start=1):
            try:
                self.triage_one(conn, message, stats)
            except Exception as exc:  # one bad message must not lose the batch
                stats.errors.append(f"{message.message_id}: {exc}")
            if progress is not None:
                progress(index, len(ordered), message)
            if index % commit_every == 0:
                conn.commit()
        conn.commit()
        derived = refresh_derived(conn, self.pack)
        stats.cases_transitioned += derived["transitioned"]
        _record_run(conn, self.pack.name, stats)
        return stats


def _record_run(conn: sqlite3.Connection, pack: str, stats: TriageStats) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            "INSERT INTO runs(started_at, finished_at, pack, processed, by_stage, notes) "
            "VALUES (?,?,?,?,?,?)",
            (
                now, now, pack, stats.processed,
                repr(dict(stats.by_stage)),
                f"llm_calls={stats.llm_calls} escalated={stats.escalated}",
            ),
        )


def build_pipeline(
    conn: sqlite3.Connection,
    pack: Pack,
    config,  # sift.config.Config — imported lazily to keep this module light
    *,
    use_llm: bool = True,
) -> tuple[Pipeline, list[str]]:
    """Assemble a pipeline from config, reporting anything unavailable.

    Nothing here is fatal. A missing model or a missing optional dependency
    degrades the pipeline to the stages that do work, and says so.
    """
    from sift.classify.features import build_vectoriser
    from sift.classify.model import ClassifierBundle
    from sift.llm.client import OllamaClient, detect_tier

    notes: list[str] = []

    bundle = ClassifierBundle.load(config.model_path)
    vectoriser = build_vectoriser(
        config.classifier.vectoriser,
        model=config.classifier.embedding_model or "nomic-embed-text",
        host=config.llm.host,
    )
    classifier = Classifier(
        pack, bundle, vectoriser, accept_threshold=config.classifier.accept_threshold
    )
    if bundle is None:
        notes.append("classifier cold-started from pack prototypes (no labels yet)")

    llm_stage: LLMStage | None = None
    if use_llm and config.llm.enabled:
        try:
            client = OllamaClient(
                config.llm.host, timeout=config.llm.timeout, allow_remote=config.llm.allow_remote
            )
            model = config.llm.model or detect_tier().model
            if not client.available():
                notes.append(
                    f"no local model backend at {config.llm.host}; running rules + classifier only"
                )
            elif not client.has_model(model):
                notes.append(f"model '{model}' is not pulled yet — run: ollama pull {model}")
            else:
                llm_stage = LLMStage(pack, client, model=model, num_ctx=config.llm.num_ctx)
        except LLMError as exc:
            notes.append(str(exc))
    elif not use_llm:
        notes.append("local model disabled for this run")

    pipeline = Pipeline(
        pack,
        classifier=classifier,
        llm=llm_stage,
        recipe_dir=config.recipe_dir,
        use_llm=llm_stage is not None,
        max_llm_calls=config.llm.max_calls_per_run,
        distil=config.classifier.distil_after_backfill,
    )
    return pipeline, notes
