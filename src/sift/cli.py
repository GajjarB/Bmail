"""The `sift` command line.

The core is headless and importable; this is a client of it, not a container for
it. Every capability is reachable from here — which is what makes the engine
scriptable, testable, and usable as a tool by something else later.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sift import __version__
from sift.config import Config, write_template
from sift.errors import SiftError
from sift.packs import Pack, available_packs, load_pack
from sift.store import repo
from sift.store.db import connect

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _load(args: argparse.Namespace) -> tuple[Config, Pack, sqlite3.Connection]:
    config = Config.load(getattr(args, "config", None))
    if getattr(args, "home", None):
        config.home = Path(args.home).expanduser()
    if getattr(args, "db", None):
        config.db_path = Path(args.db).expanduser()
    config.ensure_home()
    pack = load_pack(getattr(args, "pack", None) or config.pack, tuple(config.pack_dirs))
    return config, pack, connect(config.database)


def _own_addresses(config: Config, conn: sqlite3.Connection) -> set[str]:
    return set(config.owner_addresses) | repo.own_addresses(conn)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_init(args: argparse.Namespace) -> int:
    config = Config.load(getattr(args, "config", None))
    if args.home:
        config.home = Path(args.home).expanduser()
    config.ensure_home()
    path = write_template(config.config_path)
    conn = connect(config.database)
    conn.close()
    _out(f"config   {path}")
    _out(f"database {config.database}")
    _out("")
    _out("Next: generate a sample corpus and run the whole pipeline offline —")
    _out("  sift samples --out ./sample")
    _out("  sift ingest --path ./sample/sample.mbox --account demo")
    _out("  sift triage && sift brief")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from sift.llm.client import DEFAULT_HOST, OllamaClient, detect_tier, has_gpu

    config = Config.load(getattr(args, "config", None))
    _out(f"sift {__version__}   python {sys.version.split()[0]}   {sys.platform}")
    _out("")

    _out("store")
    try:
        conn = connect(config.database)
        counts = repo.count_messages(conn)
        _out(f"  ok       {config.database}")
        _out(f"           {counts['total']} messages ({counts['inbound']} inbound)")
        _out(f"           sqlite {sqlite3.sqlite_version}, FTS5 available")
        conn.close()
    except SiftError as exc:
        _out(f"  FAILED   {exc}")

    _out("")
    _out("optional dependencies")
    for module, purpose in (
        ("numpy", "trainable classifier head"),
        ("sklearn", "calibrated logistic regression"),
        ("selectolax", "faster, more robust HTML extraction"),
        ("sentence_transformers", "stronger multilingual embeddings"),
        ("msal", "Microsoft Graph sign-in"),
        ("keyring", "passwords in the OS keychain"),
    ):
        try:
            __import__(module)
            _out(f"  present  {module:24} {purpose}")
        except ImportError:
            _out(f"  missing  {module:24} {purpose}")

    _out("")
    _out("local model")
    tier = detect_tier()
    _out(f"  hardware {tier.ram_gb} GB RAM, gpu={has_gpu()} -> tier '{tier.name}'")
    _out(f"           suggested model: {tier.model} ({tier.note})")
    model = config.llm.model or tier.model
    try:
        client = OllamaClient(config.llm.host, allow_remote=config.llm.allow_remote)
        if client.available():
            models = client.list_models()
            _out(f"  ok       backend at {config.llm.host}, {len(models)} model(s) pulled")
            if client.has_model(model):
                _out(f"  ok       '{model}' is available")
            else:
                _out(f"  missing  '{model}' — run: ollama pull {model}")
        else:
            _out(f"  absent   nothing serving {config.llm.host} (rules + classifier still work)")
    except SiftError as exc:
        _out(f"  FAILED   {exc}")
    if config.llm.host != DEFAULT_HOST:
        _out(f"  note     host is not the default {DEFAULT_HOST}")

    _out("")
    _out("privacy")
    _out(f"  allow_remote = {config.llm.allow_remote} "
         f"({'mail content may leave this machine' if config.llm.allow_remote else 'nothing leaves this machine'})")
    leaked = [a.get("name") for a in config.accounts if a.get("password")]
    if leaked:
        _out(f"  WARNING  passwords in plain text in the config for: {', '.join(map(str, leaked))}")
        _out("           move them with: sift accounts set-password <name>")
    else:
        _out("  ok       no plaintext passwords in the config")
    _out(f"  packs    {', '.join(sorted(available_packs(config.pack_dirs)))}")
    return 0


def cmd_samples(args: argparse.Namespace) -> int:
    from sift.samples import write_mbox

    mbox, labels = write_mbox(
        args.out, seed=args.seed, applications=args.applications, noise=args.noise
    )
    _out(f"mbox   {mbox}")
    _out(f"labels {labels}")
    _out("")
    _out(f"  sift ingest --path {mbox} --account demo")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from sift.ingest import build_source, ingest

    config, _pack, conn = _load(args)
    own = _own_addresses(config, conn)

    if args.path:
        specs = [{"name": args.account or "local", "provider": "file", "path": args.path}]
    elif args.account:
        specs = [config.resolved_account(config.account(args.account))]
    else:
        specs = [config.resolved_account(entry) for entry in config.accounts]
    if not specs:
        _err("no accounts configured. Add one to the config, or pass --path for an mbox.")
        return 2

    total = 0
    for spec in specs:
        name = spec.get("name", "?")
        try:
            source = build_source(spec, own_addresses=own)
            result = ingest(conn, source, limit=args.limit, resume=not args.reset)
        except SiftError as exc:
            _err(f"{name}: {exc}")
            continue
        total += result.fetched
        _out(f"{name}: {result.fetched} messages")
    counts = repo.count_messages(conn)
    _out(f"store now holds {counts['total']} messages ({counts['inbound']} inbound)")
    conn.close()
    return 0 if total or not specs else 1


def cmd_triage(args: argparse.Namespace) -> int:
    from sift.pipeline import build_pipeline

    config, pack, conn = _load(args)
    pipeline, notes = build_pipeline(conn, pack, config, use_llm=not args.no_llm)
    for note in notes:
        _out(f"note: {note}")

    messages = repo.iter_messages(
        conn,
        limit=args.limit,
        only_untriaged_for=None if args.all else pack.name,
        since=datetime.now(timezone.utc) - timedelta(days=args.days) if args.days else None,
    )
    if not messages:
        _out("nothing to triage (use --all to re-run over messages already triaged)")
        conn.close()
        return 0

    _out(f"triaging {len(messages)} messages with pack '{pack.name}'…")
    width = 40

    def progress(index: int, total: int, _message: Any) -> None:
        if args.quiet:
            return
        filled = int(width * index / total)
        sys.stderr.write(f"\r  [{'#' * filled}{'.' * (width - filled)}] {index}/{total}")
        sys.stderr.flush()
        if index == total:
            sys.stderr.write("\n")

    stats = pipeline.triage(conn, messages, progress=progress)
    _out("")
    _out(f"processed {stats.processed}")
    _out(f"  by stage    {dict(stats.by_stage)}")
    _out(f"  by lane     {dict(stats.by_lane)}")
    _out(f"  escalated   {stats.escalated}   model calls {stats.llm_calls}"
         f"   tokens {stats.llm_tokens}")
    _out(f"  cases       {stats.cases_created} new, {stats.cases_transitioned} moved state")
    if stats.needs_review:
        _out(f"  review      {stats.needs_review} flagged — see: sift review")
    for error in stats.errors[:5]:
        _err(f"  error: {error}")
    if config.classifier.distil_after_backfill:
        classifier = pipeline.classifier
        if classifier is not None:
            outcome = classifier.train(conn)
            if outcome.get("trained"):
                classifier.save(str(config.model_path))
                _out(f"  classifier  retrained on {outcome['rows']} rows, "
                     f"holdout accuracy {outcome['holdout_accuracy']}")
    conn.close()
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    from sift.outputs.brief import build_brief, render_markdown, render_text

    _config, pack, conn = _load(args)
    brief = build_brief(conn, pack, window_days=args.days, limit=args.limit)
    if args.format == "json":
        _out(json.dumps(brief.model_dump(mode="json"), indent=2, ensure_ascii=False))
    elif args.format == "md":
        _out(render_markdown(brief))
    else:
        _out(render_text(brief))
    conn.close()
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    _config, pack, conn = _load(args)
    cases = repo.list_cases(
        conn, pack.name, state=args.state, stalled=True if args.stalled else None,
        open_only=args.open,
    )
    if args.id:
        case = next((c for c in cases if c.id == args.id), None)
        if case is None:
            _err(f"no case {args.id} in pack '{pack.name}'")
            conn.close()
            return 1
        _out(f"#{case.id}  {case.title}")
        _out(f"  state {case.state}   messages {case.message_count}   "
             f"silent {case.days_silent()} d   stalled={case.stalled}")
        if case.next_action_due:
            _out(f"  next action {case.next_action} due {case.next_action_due}")
        if case.event_datetime:
            _out(f"  event {case.event_datetime:%a %d %b %Y %H:%M UTC}")
        _out("")
        for entry in repo.case_timeline(conn, case.id):
            _out(f"  {entry['date_utc'][:10]}  {(entry['category'] or '-'):18} "
                 f"{entry['subject'][:60]}")
            if entry["evidence"]:
                _out(f"              evidence: {entry['evidence'][:100]}")
        conn.close()
        return 0

    if not cases:
        _out("no cases yet — run: sift triage")
        conn.close()
        return 0
    _out(f"{'id':>4}  {'state':20} {'org':26} {'role':26} {'msgs':>4} {'silent':>6}")
    _out("-" * 96)
    for case in cases:
        silent = case.days_silent()
        flag = " !" if case.stalled else ""
        _out(
            f"{case.id or 0:>4}  {case.state[:20]:20} {(case.org or '')[:26]:26} "
            f"{(case.role or '')[:26]:26} {case.message_count:>4} "
            f"{'' if silent is None else silent:>6}{flag}"
        )
    _out("")
    _out(f"{len(cases)} cases   ({sum(1 for c in cases if c.stalled)} stalled)")
    conn.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Confirm or correct low-confidence verdicts; each answer trains the classifier."""
    from sift.classify.model import ClassifierBundle
    from sift.classify.stage2 import Classifier

    config, pack, conn = _load(args)
    rows = conn.execute(
        "SELECT v.*, m.subject, m.from_addr FROM verdicts v "
        "JOIN messages m ON m.message_id = v.message_id "
        "WHERE v.pack = ? AND v.needs_review = 1 ORDER BY v.created_at DESC LIMIT ?",
        (pack.name, args.limit),
    ).fetchall()
    if not rows:
        _out("nothing flagged for review")
        conn.close()
        return 0

    categories = pack.category_names()
    classifier = Classifier(pack, ClassifierBundle.load(config.model_path))
    corrected = 0
    for row in rows:
        _out("")
        _out(f"from: {row['from_addr']}")
        _out(f"subj: {row['subject']}")
        _out(f"guess: {row['category']} (confidence {row['confidence']:.2f})")
        if args.dry_run:
            continue
        _out("categories: " + ", ".join(f"{i}={c}" for i, c in enumerate(categories)))
        try:
            answer = input("correct category [enter = accept, q = quit]: ").strip()
        except (EOFError, KeyboardInterrupt):
            _out("")
            break
        if answer.lower() == "q":
            break
        category = row["category"]
        if answer.isdigit() and int(answer) < len(categories):
            category = categories[int(answer)]
        elif answer in categories:
            category = answer
        message = repo.get_message(conn, row["message_id"])
        if message is not None:
            repo.add_training_row(conn, row["message_id"], pack.name, category, source="user")
            classifier.learn(message, category)
            conn.execute(
                "UPDATE verdicts SET category = ?, needs_review = 0, stage = 'manual' "
                "WHERE message_id = ? AND pack = ?",
                (category, row["message_id"], pack.name),
            )
            corrected += 1
    conn.commit()
    if corrected:
        classifier.save(str(config.model_path))
        outcome = classifier.train(conn)
        _out("")
        _out(f"{corrected} corrections recorded")
        _out(f"classifier: {outcome}")
    conn.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from sift.outputs.ics import export_ics
    from sift.outputs.tabular import export_csv, export_xlsx

    _config, pack, conn = _load(args)
    if args.xlsx:
        _out(f"xlsx {export_xlsx(conn, pack, args.xlsx)}")
    if args.csv:
        for path in export_csv(conn, pack, args.csv):
            _out(f"csv  {path}")
    if args.ics:
        _out(f"ics  {export_ics(conn, pack, args.ics)}")
    if not (args.xlsx or args.csv or args.ics):
        _err("nothing to do: pass --xlsx, --csv and/or --ics")
        conn.close()
        return 2
    conn.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    _config, _pack, conn = _load(args)
    rows = repo.search(conn, args.query, limit=args.limit)
    if not rows:
        _out("no matches")
    for row in rows:
        _out(f"{row['date_utc'][:10]}  {row['from_addr'][:34]:36} {row['subject'][:50]}")
        _out(f"            {row['snippet']}")
    conn.close()
    return 0


def cmd_packs(args: argparse.Namespace) -> int:
    config = Config.load(getattr(args, "config", None))
    packs = available_packs(config.pack_dirs)
    if args.name:
        pack = load_pack(args.name, tuple(config.pack_dirs))
        _out(f"{pack.name} — {pack.description}")
        _out(f"  entity {pack.entity}   key {' + '.join(pack.key)}   stall after {pack.stall_days} d")
        _out(f"  states {' -> '.join(pack.states)}")
        _out(f"  terminal {', '.join(pack.terminal_states) or '-'}")
        _out("  categories:")
        for name, spec in pack.categories.items():
            flags = []
            if spec.action_required:
                flags.append("action")
            if spec.high_stakes:
                flags.append("always escalate")
            if spec.state:
                flags.append(f"-> {spec.state}")
            _out(f"    {name:20} {('[' + ', '.join(flags) + ']') if flags else '':32} "
                 f"{spec.description[:52]}")
        return 0
    for name in packs:
        pack = load_pack(name, tuple(config.pack_dirs))
        _out(f"{name:22} {len(pack.categories):2} categories  {len(pack.states):2} states  "
             f"— {pack.description}")
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    """Coverage of the deterministic recipes — the number that decides how much
    model you need."""
    from sift.rules.templates import load_recipes, match_template

    config, pack, conn = _load(args)
    recipes = load_recipes(config.recipe_dir)
    if args.list:
        for recipe in recipes:
            _out(f"{recipe.priority:3} {recipe.id:34} {recipe.category:20} {recipe.vendor}")
        _out(f"\n{len(recipes)} recipes")
        conn.close()
        return 0

    messages = repo.iter_messages(conn, limit=args.limit)
    hits: dict[str, int] = {}
    matched = 0
    for message in messages:
        hit = match_template(message, config.recipe_dir)
        if hit is not None:
            matched += 1
            hits[hit.recipe.id] = hits.get(hit.recipe.id, 0) + 1
    total = len(messages) or 1
    _out(f"{matched}/{len(messages)} messages matched a recipe ({matched / total:.1%}) "
         f"— those cost zero model calls")
    for recipe_id, count in sorted(hits.items(), key=lambda kv: -kv[1]):
        _out(f"  {count:5}  {recipe_id}")
    unmatched = len(messages) - matched
    if unmatched:
        _out(f"\n{unmatched} unmatched. Top senders worth a new recipe:")
        domains: dict[str, int] = {}
        for message in messages:
            if match_template(message, config.recipe_dir) is None:
                domains[message.sender_domain] = domains.get(message.sender_domain, 0) + 1
        for domain, count in sorted(domains.items(), key=lambda kv: -kv[1])[:12]:
            _out(f"  {count:5}  {domain}")
    conn.close()
    return 0


def cmd_labels(args: argparse.Namespace) -> int:
    from sift.outputs.labels import apply_labels, plan_labels

    config, pack, conn = _load(args)
    if args.apply:
        from sift.ingest import build_source

        spec = config.resolved_account(config.account(args.apply))
        if not spec.get("allow_modify"):
            _err(f"account '{args.apply}' is not configured with allow_modify = true. "
                 "Sift will not write to a provider without that.")
            conn.close()
            return 2
        source = build_source(spec)
        result = apply_labels(conn, source, limit=args.limit, dry_run=not args.confirm)
        _out(json.dumps(result, indent=2))
        if not args.confirm:
            _out("dry run — pass --confirm to actually apply")
        conn.close()
        return 0
    plan = plan_labels(conn, pack, limit=args.limit)
    _out(f"planned {plan.planned} labels ({plan.already} already planned)")
    for label, count in sorted((plan.labels or {}).items(), key=lambda kv: -kv[1]):
        _out(f"  {count:5}  {label}")
    _out("\nNothing has been written to any provider. "
         "Apply with: sift labels --apply <account> --confirm")
    conn.close()
    return 0


def cmd_notion(args: argparse.Namespace) -> int:
    from sift.outputs.notion import sync_cases

    _config, pack, conn = _load(args)
    report = sync_cases(
        conn, pack, args.database, token=args.token, dry_run=not args.confirm, limit=args.limit
    )
    if not args.confirm:
        _out(f"dry run: {report.skipped} cases would be synced to database {args.database}")
        _out("pass --confirm to write")
    else:
        _out(f"created {report.created}, updated {report.updated}")
        for error in report.errors[:10]:
            _err(f"  {error}")
    conn.close()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from sift.evaluation import Dataset, evaluate, run_ablations
    from sift.pipeline import build_pipeline

    config, pack, conn = _load(args)
    dataset = Dataset.load(args.labels)
    if args.describe:
        _out(dataset.stratification_report())
        conn.close()
        return 0

    train, test = dataset.split(test_fraction=args.test_fraction)
    subset = test if not args.full else dataset
    _out(f"dataset {len(dataset)} labelled · evaluating on "
         f"{'all' if args.full else f'the locked test split ({len(test)}; {len(train)} held for training)'}")
    _out("")

    pipeline, notes = build_pipeline(conn, pack, config, use_llm=not args.no_llm)
    for note in notes:
        _out(f"note: {note}")

    if args.ablate:
        suite = run_ablations(
            conn, pack, subset,
            classifier=pipeline.classifier, llm=pipeline.llm, recipe_dir=config.recipe_dir,
        )
        _out("")
        _out(suite.table())
        if args.out:
            Path(args.out).expanduser().write_text(
                json.dumps(suite.as_dict(), indent=2), encoding="utf-8"
            )
            _out(f"\nwritten to {args.out}")
    else:
        report = evaluate(conn, pack, pipeline, subset, name=args.name)
        _out("")
        _out(report.render())
        if args.out:
            _out(f"written to {report.save(args.out)}")
    conn.close()
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    from sift.secrets import env_hint, set_secret

    config = Config.load(getattr(args, "config", None))
    if args.set_password:
        import getpass

        password = getpass.getpass(f"password for account '{args.set_password}': ")
        try:
            _out(set_secret(args.set_password, password))
        except RuntimeError as exc:
            _err(str(exc))
            return 2
        return 0
    conn = connect(config.database)
    rows = repo.list_accounts(conn)
    if not rows and not config.accounts:
        _out("no accounts. Add one to the config, or ingest an mbox with --path.")
    for entry in config.accounts:
        name = entry.get("name", "?")
        _out(f"{name:16} {entry.get('provider', '?'):8} {entry.get('address', '')}")
        if entry.get("provider") == "imap" and not entry.get("password"):
            _out(f"                 password: keychain or ${env_hint(str(name))}")
    for row in rows:
        _out(f"{row['name']:16} {row['provider']:8} last sync "
             f"{row['last_sync'] or 'never'}   cursor {row['cursor'][:48]}")
    conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from sift.api import serve

    config = Config.load(getattr(args, "config", None))
    return serve(config, host=args.host, port=args.port, pack=args.pack)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sift",
        description="Local-first email triage. Nothing leaves this machine.",
    )
    parser.add_argument("--version", action="version", version=f"sift {__version__}")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--home", help="override the data directory")
    parser.add_argument("--db", help="override the database path")
    parser.add_argument("--pack", help="domain pack to use for this command")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create the config file and the store")
    init.set_defaults(func=cmd_init)

    doctor = subparsers.add_parser("doctor", help="check the environment and privacy settings")
    doctor.set_defaults(func=cmd_doctor)

    samples = subparsers.add_parser(
        "samples", help="generate a synthetic corpus so you can run everything offline"
    )
    samples.add_argument("--out", default="./sample", help="output directory")
    samples.add_argument("--seed", type=int, default=7)
    samples.add_argument("--applications", type=int, default=24)
    samples.add_argument("--noise", type=int, default=40)
    samples.set_defaults(func=cmd_samples)

    ingest = subparsers.add_parser("ingest", help="pull messages into the store")
    ingest.add_argument("--path", help="an mbox file, Maildir or directory of .eml files")
    ingest.add_argument("--account", help="account name from the config")
    ingest.add_argument("--limit", type=int, help="stop after N messages")
    ingest.add_argument("--reset", action="store_true", help="ignore the stored sync cursor")
    ingest.set_defaults(func=cmd_ingest)

    triage = subparsers.add_parser("triage", help="run the pipeline over untriaged messages")
    triage.add_argument("--limit", type=int)
    triage.add_argument("--days", type=int, help="only messages from the last N days")
    triage.add_argument("--all", action="store_true", help="re-triage everything")
    triage.add_argument("--no-llm", action="store_true", help="rules and classifier only")
    triage.add_argument("--quiet", action="store_true")
    triage.set_defaults(func=cmd_triage)

    brief = subparsers.add_parser("brief", help="what needs you, in one screen")
    brief.add_argument("--days", type=int, default=1, help="window in days")
    brief.add_argument("--limit", type=int, default=8)
    brief.add_argument("--format", choices=("text", "md", "json"), default="text")
    brief.set_defaults(func=cmd_brief)

    cases = subparsers.add_parser("cases", help="the tracker")
    cases.add_argument("--id", type=int, help="show one case with its full timeline")
    cases.add_argument("--state")
    cases.add_argument("--stalled", action="store_true", help="only cases that have gone quiet")
    cases.add_argument("--open", action="store_true", help="hide closed cases")
    cases.set_defaults(func=cmd_cases)

    review = subparsers.add_parser("review", help="confirm low-confidence verdicts")
    review.add_argument("--limit", type=int, default=20)
    review.add_argument("--dry-run", action="store_true", help="list without prompting")
    review.set_defaults(func=cmd_review)

    export = subparsers.add_parser("export", help="write Excel, CSV and calendar files")
    export.add_argument("--xlsx")
    export.add_argument("--csv", help="output directory")
    export.add_argument("--ics")
    export.set_defaults(func=cmd_export)

    search = subparsers.add_parser("search", help="full-text search over the store")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=cmd_search)

    packs = subparsers.add_parser("packs", help="list or describe domain packs")
    packs.add_argument("name", nargs="?")
    packs.set_defaults(func=cmd_packs)

    templates = subparsers.add_parser("templates", help="recipe coverage over your own corpus")
    templates.add_argument("--list", action="store_true")
    templates.add_argument("--limit", type=int, default=2000)
    templates.set_defaults(func=cmd_templates)

    labels = subparsers.add_parser("labels", help="plan (and optionally apply) provider labels")
    labels.add_argument("--apply", metavar="ACCOUNT", help="apply the plan through this account")
    labels.add_argument("--confirm", action="store_true", help="actually write; otherwise dry run")
    labels.add_argument("--limit", type=int)
    labels.set_defaults(func=cmd_labels)

    notion = subparsers.add_parser("notion", help="sync cases into an existing Notion database")
    notion.add_argument("--database", required=True, help="target database id")
    notion.add_argument("--token", help="defaults to $NOTION_TOKEN")
    notion.add_argument("--confirm", action="store_true")
    notion.add_argument("--limit", type=int)
    notion.set_defaults(func=cmd_notion)

    evaluate_parser = subparsers.add_parser("eval", help="score the pipeline against labels")
    evaluate_parser.add_argument("labels", help="JSONL label file")
    evaluate_parser.add_argument("--name", default="run")
    evaluate_parser.add_argument("--out", help="write the report as JSON")
    evaluate_parser.add_argument("--ablate", action="store_true", help="run the ablation suite")
    evaluate_parser.add_argument("--describe", action="store_true", help="just describe the dataset")
    evaluate_parser.add_argument("--full", action="store_true",
                                 help="evaluate on everything, not the locked test split")
    evaluate_parser.add_argument("--test-fraction", type=float, default=0.3)
    evaluate_parser.add_argument("--no-llm", action="store_true")
    evaluate_parser.set_defaults(func=cmd_eval)

    accounts = subparsers.add_parser("accounts", help="list accounts, store passwords")
    accounts.add_argument("--set-password", metavar="ACCOUNT")
    accounts.set_defaults(func=cmd_accounts)

    serve = subparsers.add_parser("serve", help="local read-only API on 127.0.0.1")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0, help="0 picks a free port")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SiftError as exc:
        _err(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        _err("\ninterrupted")
        return 130
    except BrokenPipeError:
        # `sift cases | head` closes the pipe early; that is not an error.
        # Redirect stdout so the interpreter's own flush at exit stays quiet.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
