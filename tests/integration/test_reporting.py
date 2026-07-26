"""Integration tests for honest run export and PDF packaging primitives."""

import zipfile
from datetime import UTC, datetime

from pypdf import PdfReader

from ecoloop.config import repository_root
from ecoloop.db.store import SQLiteStore
from ecoloop.reporting import (
    _clear_stale_submission_artifacts,
    _write_source_zip,
    export_run,
    render_text_pdf,
)
from ecoloop.schemas import RunType


def test_render_text_pdf_is_reopenable_and_contains_title(tmp_path):
    source = tmp_path / "report.md"
    source.write_text(
        "# Internal heading\n\n## Evidence\n\n- Real data only.\n- No invented savings.\n",
        encoding="utf-8",
    )
    output = render_text_pdf(
        source,
        tmp_path / "report.pdf",
        document_title="EcoLoop Verification Report",
    )
    reader = PdfReader(str(output))
    assert len(reader.pages) >= 1
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "EcoLoop Verification Report" in text
    assert "No invented savings." in text


def test_export_run_marks_explicit_fake_and_writes_empty_valid_exports(tmp_path):
    database = tmp_path / "runs" / "ecoloop.db"
    store = SQLiteStore(database)
    store.create_run(
        "fake-ci-run",
        RunType.RULE,
        is_fake=True,
        period_name="smoke",
        timestamp=datetime(2026, 7, 15, tzinfo=UTC),
    )
    comparison_json = tmp_path / "verified-comparison.json"
    comparison_csv = tmp_path / "verified-comparison.csv"
    comparison_json.write_text('{"status":"fixture"}\n', encoding="utf-8")
    comparison_csv.write_text("status\nfixture\n", encoding="utf-8")
    store.record_artifact("fake-ci-run", "comparison_json", comparison_json)
    store.record_artifact("fake-ci-run", "comparison_csv", comparison_csv)
    output = export_run(database, tmp_path / "runs", "fake-ci-run")
    run_payload = (output / "run.json").read_text(encoding="utf-8")
    assert "EXPLICIT_TEST_FAKE" in run_payload
    assert (output / "telemetry.csv").read_text(encoding="utf-8").startswith("telemetry_id,")
    assert (output / "metrics.json").read_text(encoding="utf-8").strip() == "{}"
    assert (output / "comparison.json").read_text(encoding="utf-8") == '{"status":"fixture"}\n'
    assert (output / "comparison.csv").read_text(encoding="utf-8") == "status\nfixture\n"


def test_export_run_is_idempotent_when_comparison_is_already_in_export_directory(
    tmp_path,
):
    database = tmp_path / "runs" / "ecoloop.db"
    store = SQLiteStore(database)
    store.create_run(
        "real-agent-run",
        RunType.AGENT,
        is_fake=False,
        period_name="smoke",
        timestamp=datetime(2026, 7, 15, tzinfo=UTC),
    )
    export_directory = tmp_path / "runs" / "real-agent-run" / "export"
    export_directory.mkdir(parents=True)
    comparison_json = export_directory / "comparison.json"
    comparison_csv = export_directory / "comparison.csv"
    comparison_json.write_text('{"status":"verified"}\n', encoding="utf-8")
    comparison_csv.write_text("status\nverified\n", encoding="utf-8")
    store.record_artifact("real-agent-run", "comparison_json", comparison_json)
    store.record_artifact("real-agent-run", "comparison_csv", comparison_csv)

    output = export_run(database, tmp_path / "runs", "real-agent-run")

    assert output == export_directory.resolve()
    assert comparison_json.read_text(encoding="utf-8") == '{"status":"verified"}\n'
    assert comparison_csv.read_text(encoding="utf-8") == "status\nverified\n"


def test_source_zip_uses_release_allowlist_and_excludes_runtime_data(tmp_path):
    archive_path = tmp_path / "ecoloop-source.zip"
    _write_source_zip(repository_root(), archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())

    assert "README.md" in names
    assert "THIRD_PARTY_NOTICES.md" in names
    assert "models/generated/agent_replay.idf" in names
    assert "models/generated/preparation-manifest.json" in names
    assert not any(name.startswith(("runs/", "submission/", ".git/", ".venv/")) for name in names)
    assert not any(name.casefold().endswith((".epw", ".gguf", ".exe", ".key")) for name in names)
    assert not any(name.startswith(".env.") and name != ".env.example" for name in names)


def test_packaging_removes_only_known_stale_outputs(tmp_path):
    stale = tmp_path / "presentation.pdf"
    keep = tmp_path / "review-notes.txt"
    stale.write_bytes(b"old")
    keep.write_text("keep", encoding="utf-8")

    _clear_stale_submission_artifacts(tmp_path)

    assert not stale.exists()
    assert keep.read_text(encoding="utf-8") == "keep"
