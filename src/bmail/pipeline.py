"""The triage pipeline: Stage 1 -> 2 -> 3 -> 4, with honest accounting.

Every message enters at Stage 1 and leaves as soon as something is confident
enough to settle it. The `TriageStats` returned at the end reports where each
message actually stopped, which is the number that tells you whether the
expensive stage is earning its keep.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bmail.classify.stage2 import Classifier
from bmail.entities.resolve import is_ats_domain
from bmail.entities.stage4 import CaseResolver, refresh_derived
from bmail.errors import LLMError
from bmail.llm.stage3 import LLMStage
from bmail.models import Lane, Message, Stage, Verdict
from bmail.packs import Pack
from bmail.rules.engine import RulesEngine, Stage1Result
from bmail.store import repo
from bmail.store.db import get_meta, set_meta

ProgressHook = Callable[[int, int, Message], None]

# What the foot of a mailing says, and one person's mail to another never does.
_MAILING_FOOTER = re.compile(
    r"unsubscribe|opt[- ]out|e-?mail preferences|notification settings|"
    r"manage (?:your )?(?:preferences|subscriptions?)|abmelden|abbestellen|austragen",
    re.IGNORECASE,
)
# Shortened links have lost their scheme, so a host with a path counts too.
_LINK = re.compile(r"https?://\S+|\b[\w-]+(?:\.[\w-]+)+/\S*")
_MANY_LINKS = 5
_BROADCAST_MAILBOX = re.compile(
    r"^(?:hello|hi|hey|yo|team|news|newsletter|notify|info|marketing|community|digest|"
    r"updates?|mail|mailer|email|contact|connect|discover|recommendations?|offers?|deals?|"
    r"promo\w*|learn|academy|events?)$",
    re.IGNORECASE,
)

# Raise this whenever the rules, the recipes or a pack would sort stored mail
# differently: the app sorts everything it holds again, once, after an update.
# Answers a person typed in are kept.
TRIAGE_VERSION = 2


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
        existing = None
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
        kept = False

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
                if verdict.stage is Stage.classifier:
                    if existing is not None and existing.stage is Stage.llm:
                        # The local model read this one on an earlier run and
                        # cannot be asked now (switched off, or this run's
                        # calls are spent). Its reading beats today's guess.
                        verdict, kept = existing, True
                    else:
                        self._when_unsure(message, stage1, verdict)
            # Feed the settled answer forward so later duplicates inherit it.
            self.rules.remember(message, verdict)
            if (self.distil and verdict.stage is Stage.llm and not verdict.needs_review
                    and not kept):
                # Distillation: the expensive stage teaches the cheap one, so the
                # same mail shape costs nothing next month.
                repo.add_training_row(
                    conn, message.message_id, self.pack.name, verdict.category,
                    verdict.action_required, source="distilled",
                )
        elif not stage1.resolved:
            verdict.needs_review = True
            verdict.reasons.append("no classifier configured")

        if verdict.stage in (Stage.rules, Stage.classifier):
            self._keep_to_topic(message, stage1, verdict)
        repo.save_verdict(conn, verdict)
        self._settle(conn, message, verdict, stats)
        return verdict

    # Below this machine score, a message reads as written by a person. Any one
    # machine signal is enough to cross it: a no-reply or system sender, a list
    # header, an auto-reply. A bank, a shop or a security alert never writes to
    # you as a person does, and must never be shown as if it had.
    HUMAN_BELOW = 0.25

    def _written_by_a_person(self, message: Message, stage1: Stage1Result) -> bool:
        headers = stage1.headers
        if headers.score >= self.HUMAN_BELOW:
            return False
        # Carried by a sending service and nothing else: a recruiter writing
        # through an applicant system arrives like this, and so does every shop
        # and every digest. What it is about tells them apart, and whether it
        # offers a way off its list.
        return not headers.is_relayed or (self._on_topic(message) and not _is_mailing(message))

    def _on_topic(self, message: Message) -> bool:
        return self.pack.is_on_topic(f"{message.subject}\n{message.body_text}")

    def _when_unsure(self, message: Message, stage1: Stage1Result, verdict: Verdict) -> None:
        """Nothing settled this message; a guess must not pass for a reading.

        With no local model — the default install — an unsure classifier guess
        used to be the final answer. On day one that guess comes from a handful
        of prototype sentences, so a friend asking "coffee on Friday?" was filed
        as an automatic acknowledgement and never shown: the one outcome the
        asymmetric-routing rule exists to prevent. A message a person wrote,
        which the classifier cannot place with confidence and would otherwise
        file as needing nothing, is surfaced as personal mail instead.

        The same guess about mail a machine sent failed the other way: a DSL
        order, a bank notice and a freelance platform's welcome series were
        shown as job offers to answer today, each on a probability barely above
        chance. Software that wants something from you says so in words a
        recipe knows; an unsure guess that it does is filed as what the sender
        is. Both stay in the review queue, so a correction still teaches.
        """
        person = self._written_by_a_person(message, stage1)
        if person:
            if not verdict.action_required and "personal" in self.pack.categories:
                self._refile(verdict, "personal", "written by a person and the guess "
                             f"'{verdict.category}' is unsure: shown, not filed")
            return
        # A guess that only files machine mail somewhere quiet does no harm.
        # One that says it needs you, or that it is the outcome of something
        # you applied for, has to be about that.
        claims = verdict.action_required or self.pack.spec(verdict.category).on_topic
        accept = self.classifier.accept_threshold if self.classifier is not None else 1.0
        if not claims or verdict.confidence >= accept:
            return
        # One kind of software does write to you alone about the pack's
        # subject: an applicant system, or a company's own no-reply mailbox.
        # A mailing does not, and a job board never sends the outcome of one.
        # (A campaign header alone is no mailing: the large sending services
        # stamp one on every message, an interview invitation included.)
        headers = stage1.headers
        mailing = headers.is_list or headers.is_campaign or _is_mailing(message)
        if (self._on_topic(message) and not self.rules.is_job_board_mail(message)
                and (not mailing or is_ats_domain(message.sender_domain))):
            return
        fallback = self.rules.category_for_machine_mail(message, stage1.headers)
        self._refile(verdict, fallback, f"sent by a machine and the guess '{verdict.category}' "
                     f"is unsure: filed as {fallback}")

    def _refile(self, verdict: Verdict, category: str, reason: str) -> None:
        spec = self.pack.spec(category)
        verdict.category = category
        verdict.action_required = spec.action_required
        verdict.urgency = spec.urgency
        verdict.next_action = spec.next_action
        verdict.needs_review = True
        verdict.reasons.append(reason)

    def _keep_to_topic(self, message: Message, stage1: Stage1Result, verdict: Verdict) -> None:
        """Only mail about the pack's subject gets the pack's subject categories.

        A rule matches a phrase and the classifier matches a shape; neither knows
        that "we regret to inform you that your credit card application" is not
        about a job. The pack does: its topic words. Without one, an outcome of
        an application becomes what the sender makes it: housekeeping from a
        machine, or personal mail from a person, which is still shown to you.
        And mail from a machine is never "personal".
        """
        machine = (
            not self._written_by_a_person(message, stage1)
            or verdict.lane in (Lane.bulk, Lane.template)
        )
        spec = self.pack.spec(verdict.category)
        if spec.on_topic and not self.pack.is_on_topic(f"{message.subject}\n{message.body_text}"):
            reason = f"'{verdict.category}' without a word about the subject"
        elif verdict.category == "personal" and machine:
            reason = "sent by a machine, not a person"
        else:
            return
        fallback = "admin" if machine else "personal"
        if fallback not in self.pack.categories:
            fallback = "other"
        target = self.pack.spec(fallback)
        verdict.reasons.append(f"{reason}: filed as {fallback}")
        verdict.category = fallback
        verdict.action_required = target.action_required
        verdict.urgency = target.urgency
        verdict.next_action = target.next_action
        verdict.org = verdict.role = None

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
        fresh: bool = False,
    ) -> TriageStats:
        """Sort `messages`. `fresh` says they are the whole store, being sorted
        again: what an earlier run decided is then no reference for a repeat,
        because that earlier answer is exactly what is being replaced."""
        stats = TriageStats()
        self.rules.trust_since = repo._dt_to_text(datetime.now(timezone.utc)) if fresh else None
        first = not _sorted_before(conn, self.pack.name)
        if fresh:
            # The cards are built from the answers being replaced, and a card
            # only ever moves forward: one opened by a digest misread as an
            # assessment would stay "Indeed, assessment" for good. They hold
            # nothing a person typed (one with notes is kept), so they are
            # built again from the mail.
            conn.execute("DELETE FROM cases WHERE pack = ? AND notes = ''", (self.pack.name,))
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
        if fresh or first:
            # Every answer the store now holds was given by these rules.
            set_meta(conn, _version_key(self.pack.name), str(TRIAGE_VERSION))
        return stats


def _is_mailing(message: Message) -> bool:
    """A way off the list, and a second sign of one: a digest or a promotion.

    Two signs, because the footer alone is also true of mail that matters: a
    recruiter's applicant system puts "unsubscribe" under a three-line note.
    The second is a page of links, or a mailbox that greets everyone at once
    (hello@, news@, notify@) rather than a person's or a hiring desk's.
    """
    text = message.body_raw_text or message.body_text
    if _MAILING_FOOTER.search(text) is None:
        return False
    local = message.from_.address.split("@")[0]
    return (len(_LINK.findall(text)) >= _MANY_LINKS
            or _BROADCAST_MAILBOX.match(local) is not None)


def _version_key(pack: str) -> str:
    return f"triage_version:{pack}"


def _sorted_before(conn: sqlite3.Connection, pack: str) -> bool:
    # An answer a person typed in was not given by any version of the rules.
    row = conn.execute(
        "SELECT 1 FROM verdicts WHERE pack = ? AND stage != 'manual' LIMIT 1", (pack,)).fetchone()
    return row is not None


def sorted_by_older_rules(conn: sqlite3.Connection, pack: str) -> bool:
    """Whether the store holds answers an earlier version of the rules gave.

    Mail sorted by an older version keeps that version's answers until it is
    sorted again, and nobody thinks to ask for that after an update: the
    mistakes a release fixed stayed on screen.
    """
    return _sorted_before(conn, pack) and get_meta(conn, _version_key(pack)) != str(TRIAGE_VERSION)


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
    config,  # bmail.config.Config — imported lazily to keep this module light
    *,
    use_llm: bool = True,
) -> tuple[Pipeline, list[str]]:
    """Assemble a pipeline from config, reporting anything unavailable.

    Nothing here is fatal. A missing model or a missing optional dependency
    degrades the pipeline to the stages that do work, and says so.
    """
    from bmail.classify.features import build_vectoriser
    from bmail.classify.model import ClassifierBundle
    from bmail.llm.client import OllamaClient, detect_tier

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
