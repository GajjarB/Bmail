from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from sift.classify.stage2 import Classifier
from sift.evaluation import Dataset, evaluate, run_ablations
from sift.evaluation.metrics import (
    binary_metrics,
    classification_report,
    exact_date_equal,
    fuzzy_equal,
    precision_at_k,
)
from sift.ingest import FileSource, ingest
from sift.outputs.brief import build_brief, render_markdown, render_text
from sift.outputs.ics import build_calendar, export_ics
from sift.outputs.labels import plan_labels
from sift.outputs.tabular import export_csv, export_xlsx, overview
from sift.pipeline import Pipeline
from sift.samples import write_mbox
from sift.store import repo


@pytest.fixture()
def triaged(conn, pack, tmp_path):
    mbox, labels = write_mbox(tmp_path, applications=14, noise=20)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(
        conn, repo.iter_messages(conn)
    )
    return conn, pack, labels


# --- Brief -------------------------------------------------------------------- #


def test_brief_ranks_and_renders(triaged):
    conn, pack, _ = triaged
    brief = build_brief(conn, pack, window_days=400, limit=5)
    assert brief.counts["needs_you"] <= 5
    scores = [item.score for item in brief.needs_you]
    assert scores == sorted(scores, reverse=True)
    for renderer in (render_text, render_markdown):
        text = renderer(brief)
        assert "need" in text.lower() or "Needs you" in text
        assert text.endswith("\n")


def test_brief_json_is_serialisable(triaged):
    conn, pack, _ = triaged
    payload = build_brief(conn, pack, window_days=400).model_dump(mode="json")
    assert json.loads(json.dumps(payload))["pack"] == pack.name


def test_brief_only_shows_future_deadlines(triaged):
    conn, pack, _ = triaged
    today = datetime.now(timezone.utc).date()
    brief = build_brief(conn, pack, window_days=400)
    assert all(item.due is None or item.due >= today - timedelta(days=1)
               for item in brief.deadlines)


# --- Exports ------------------------------------------------------------------- #


def test_xlsx_has_the_five_sheets(triaged, tmp_path):
    conn, pack, _ = triaged
    openpyxl = pytest.importorskip("openpyxl")
    path = export_xlsx(conn, pack, tmp_path / "out.xlsx")
    workbook = openpyxl.load_workbook(path)
    assert workbook.sheetnames == ["Overview", "Cases", "Timeline", "Deadlines", "Contacts"]
    assert workbook["Cases"].max_row > 1


def test_csv_export_writes_every_table(triaged, tmp_path):
    conn, pack, _ = triaged
    paths = export_csv(conn, pack, tmp_path / "csv")
    assert {p.stem for p in paths} == {"cases", "timeline", "deadlines", "contacts"}
    assert all(p.read_text(encoding="utf-8").count("\n") >= 1 for p in paths)


def test_overview_reports_kpis(triaged):
    conn, pack, _ = triaged
    rows = dict(row[:2] for row in overview(conn, pack) if len(row) == 2)
    assert rows["Pack"] == pack.name
    assert int(rows["Messages ingested"]) > 0


def test_ics_is_well_formed(triaged, tmp_path):
    conn, pack, _ = triaged
    text = build_calendar(conn, pack)
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.rstrip().endswith("END:VCALENDAR")
    assert text.count("BEGIN:VEVENT") == text.count("END:VEVENT")
    # RFC 5545: no content line may exceed 75 octets before folding.
    for line in text.split("\r\n"):
        if line and not line.startswith(" "):
            assert len(line.encode("utf-8")) <= 75, line
    export_ics(conn, pack, tmp_path / "cal.ics")


def test_ics_escapes_special_characters(conn, pack):
    from sift.models import Case

    repo.save_case(
        conn,
        Case(pack=pack.name, key="acme|x", title="Acme; Inc, \"the\" one",
             state="applied", next_action_due=date(2030, 1, 1)),
    )
    conn.commit()
    text = build_calendar(conn, pack)
    summary = next(line for line in text.split("\r\n") if line.startswith("SUMMARY:"))
    assert "\\;" in summary and "\\," in summary


def test_labels_are_planned_but_never_applied(triaged):
    conn, pack, _ = triaged
    plan = plan_labels(conn, pack)
    assert plan.planned > 0
    assert all(label.startswith(f"{pack.label_prefix}/") for label in (plan.labels or {}))
    applied = conn.execute("SELECT COUNT(*) c FROM label_plan WHERE applied = 1").fetchone()["c"]
    assert applied == 0  # planning must never write outward


# --- Evaluation ------------------------------------------------------------------ #


def test_dataset_split_is_deterministic(tmp_path):
    _mbox, labels = write_mbox(tmp_path, applications=10, noise=10)
    dataset = Dataset.load(labels)
    a_train, a_test = dataset.split()
    b_train, b_test = dataset.split()
    assert [r.message_id for r in a_test] == [r.message_id for r in b_test]
    assert not ({r.message_id for r in a_train} & {r.message_id for r in a_test})


def test_dataset_round_trips(tmp_path):
    _mbox, labels = write_mbox(tmp_path, applications=4, noise=4)
    dataset = Dataset.load(labels)
    written = dataset.save(tmp_path / "again.jsonl")
    assert len(Dataset.load(written)) == len(dataset)


def test_evaluation_does_not_touch_the_live_store(triaged):
    conn, pack, labels = triaged
    before = repo.verdict_stats(conn, pack.name)["total"]
    cases_before = len(repo.list_cases(conn, pack.name))
    report = evaluate(
        conn, pack,
        Pipeline(pack, classifier=Classifier(pack), use_llm=False),
        Dataset.load(labels),
    )
    assert report.evaluated > 0
    assert repo.verdict_stats(conn, pack.name)["total"] == before
    assert len(repo.list_cases(conn, pack.name)) == cases_before


def test_evaluation_report_renders_and_saves(triaged, tmp_path):
    conn, pack, labels = triaged
    report = evaluate(
        conn, pack, Pipeline(pack, classifier=Classifier(pack), use_llm=False),
        Dataset.load(labels), name="unit",
    )
    text = report.render()
    assert "ACTION REQUIRED" in text and "hallucination rate" in text
    saved = json.loads(report.save(tmp_path / "r.json").read_text(encoding="utf-8"))
    assert saved["name"] == "unit"


def test_ablation_suite_compares_configurations(triaged):
    conn, pack, labels = triaged
    suite = run_ablations(
        conn, pack, Dataset.load(labels), classifier=Classifier(pack), llm=None
    )
    names = [row.name for row in suite.rows]
    assert any("rules only" in name for name in names)
    assert any("classifier" in name for name in names)
    assert "configuration" in suite.table()


def test_metric_primitives():
    report = classification_report([("a", "a"), ("a", "b"), ("b", "b")])
    assert report.accuracy == pytest.approx(2 / 3)
    assert 0 < report.macro_f1 <= 1
    assert binary_metrics([(True, False)]).recall == 0.0
    assert precision_at_k([True, False, True], 2) == 0.5
    assert exact_date_equal("2026-09-14", "2026-09-14T00:00:00")
    assert not exact_date_equal("2026-09-14", "2026-09-15")
    assert fuzzy_equal("Acme GmbH", "acme")


# --- CLI --------------------------------------------------------------------------- #


def test_cli_end_to_end(tmp_path, capsys, monkeypatch):
    from sift.cli import main

    home = tmp_path / "home"
    monkeypatch.setenv("SIFT_HOME", str(home))
    common = ["--home", str(home)]

    assert main([*common, "init"]) == 0
    assert main([*common, "samples", "--out", str(tmp_path / "s"),
                 "--applications", "8", "--noise", "10"]) == 0
    assert main([*common, "ingest", "--path", str(tmp_path / "s" / "sample.mbox"),
                 "--account", "demo"]) == 0
    assert main([*common, "triage", "--no-llm", "--quiet"]) == 0
    capsys.readouterr()

    assert main([*common, "brief", "--days", "400"]) == 0
    assert "need you" in capsys.readouterr().out

    assert main([*common, "cases"]) == 0
    assert "cases" in capsys.readouterr().out

    assert main([*common, "export", "--xlsx", str(tmp_path / "o.xlsx"),
                 "--ics", str(tmp_path / "o.ics")]) == 0
    capsys.readouterr()
    assert (tmp_path / "o.xlsx").exists() and (tmp_path / "o.ics").exists()

    assert main([*common, "eval", str(tmp_path / "s" / "sample.labels.jsonl"), "--no-llm"]) == 0
    assert "ACTION REQUIRED" in capsys.readouterr().out

    assert main([*common, "templates"]) == 0
    assert re.search(r"\d+/\d+ messages matched", capsys.readouterr().out)

    assert main([*common, "packs"]) == 0
    assert "job-seeker" in capsys.readouterr().out

    assert main([*common, "doctor"]) == 0
    output = capsys.readouterr().out
    assert "nothing leaves this machine" in output


def test_cli_export_without_a_target_is_an_error(tmp_path, monkeypatch, capsys):
    from sift.cli import main

    monkeypatch.setenv("SIFT_HOME", str(tmp_path / "home"))
    main(["--home", str(tmp_path / "home"), "init"])
    capsys.readouterr()
    assert main(["--home", str(tmp_path / "home"), "export"]) == 2


def test_cli_reports_unknown_packs_cleanly(tmp_path, monkeypatch, capsys):
    from sift.cli import main

    monkeypatch.setenv("SIFT_HOME", str(tmp_path / "home"))
    assert main(["--home", str(tmp_path / "home"), "--pack", "nope", "cases"]) == 1
    assert "unknown pack" in capsys.readouterr().err
