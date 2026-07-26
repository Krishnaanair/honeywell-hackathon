"""Integration tests for honest run export and PDF packaging primitives."""

import csv
import json
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pypdf import PdfReader

import ecoloop.reporting as reporting
from ecoloop.config import repository_root
from ecoloop.db.store import SQLiteStore
from ecoloop.exceptions import RunStateError
from ecoloop.reporting import (
    PackagingError,
    _clear_stale_submission_artifacts,
    _table_column_widths,
    _write_source_zip,
    export_run,
    render_text_pdf,
)
from ecoloop.schemas import RunStatus, RunType, ValidationResult
from tests.unit._factories import NOW, action, candidate, observation_input


def _write_fixture(root: Path, relative: str, content: str = "fixture\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_minimal_presentation(path: Path, text: str = "Verified result") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "docProps/core.xml",
            '<?xml version="1.0" encoding="UTF-8"?><metadata>EcoLoop</metadata>',
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><slide>{text}</slide>',
        )


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        shell=False,
    )


def _commit_index(root: Path, message: str = "fixture") -> None:
    _run_git(
        root,
        "-c",
        "user.name=EcoLoop Tests",
        "-c",
        "user.email=ecoloop-tests@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        message,
    )


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


def test_render_text_pdf_cleans_bounded_markdown_without_interpreting_html(tmp_path):
    source = tmp_path / "report.md"
    source.write_text(
        "# Internal heading\n\n"
        "## **Evidence**\n\n"
        "<!-- BEGIN VERIFIED_EVALUATION_BLOCK -->\n"
        "1. **Actuator proof** uses `safe-run-id`.\n"
        "- Literal source HTML stays literal: <b>not trusted</b>.\n\n"
        "See [`architecture.md`](docs/architecture.md) and "
        "[official guidance](https://example.com/guide?mode=safe&v=1).\n\n"
        "| Evidence run | Facility electricity | HVAC electricity | Peak demand | "
        "API observations | Warnings / severe / fatal |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: |\n"
        "| `real-baseline-smoke` | 217.912262 kWh | 89.437262 kWh | "
        "22.273231 kW | 96 | 1 / 0 / 0 |\n"
        "<!-- END VERIFIED_EVALUATION_BLOCK -->\n",
        encoding="utf-8",
    )

    output = render_text_pdf(
        source,
        tmp_path / "report.pdf",
        document_title="EcoLoop Markdown Cleanup",
    )

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(output)).pages)
    assert "**" not in text
    assert "`" not in text
    assert "VERIFIED_EVALUATION_BLOCK" not in text
    assert "Actuator proof uses safe-run-id." in " ".join(text.split())
    assert "real-baseline-smoke" in text
    assert "<b>not trusted</b>" in text
    assert "architecture.md" in text
    assert "official guidance" in text
    assert "](docs/architecture.md)" not in text


def test_report_table_gives_identifier_column_more_width():
    rows = [
        [
            "Evidence run",
            "Facility electricity",
            "HVAC electricity",
            "Peak demand",
            "API observations",
            "Warnings / severe / fatal",
        ],
        [
            "`real-baseline-smoke`",
            "217.912262 kWh",
            "89.437262 kWh",
            "22.273231 kW",
            "96",
            "1 / 0 / 0",
        ],
    ]

    widths = _table_column_widths(rows)

    assert len(widths) == 6
    assert widths[0] > widths[4]
    assert widths[4] > widths[3]
    assert sum(widths) == pytest.approx(174 * 72 / 25.4)


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


def test_export_run_writes_exact_replay_inputs_from_immutable_real_run(
    tmp_path: Path,
) -> None:
    run_id = "real-agent-export"
    runs_directory = tmp_path / "runs"
    run_directory = runs_directory / run_id
    input_directory = run_directory / "inputs"
    energyplus_directory = run_directory / "energyplus"
    input_directory.mkdir(parents=True)
    energyplus_directory.mkdir()
    model = input_directory / "agent_ready.idf"
    actuator_map = input_directory / "actuator_map.csv"
    api_points = energyplus_directory / "api_points.csv"
    model.write_text("Version,26.1;\n", encoding="utf-8")
    actuator_map.write_text(
        "logical_action,component_type,control_type,actuator_key,required\n"
        "heating_setpoint,Schedule:Compact,Schedule Value,Heat Schedule,true\n",
        encoding="utf-8",
    )
    api_points.write_text(
        "what,name,key,type,unit\nActuator,Schedule:Compact,Heat Schedule,Schedule Value,C\n",
        encoding="utf-8",
    )

    database = runs_directory / "ecoloop.db"
    store = SQLiteStore(database, clock=lambda: NOW)
    store.create_run(
        run_id,
        RunType.AGENT,
        is_fake=False,
        energyplus_version="26.1.0",
        model_path=model,
        period_name="smoke",
        timestamp=NOW,
    )
    store.set_run_status(run_id, RunStatus.RUNNING, timestamp=NOW)
    observed = store.record_observation(
        observation_input(
            run_id=run_id,
            simulation_timestamp=NOW,
            timestep_key="real-export-step-1",
        )
    )
    applied = candidate(
        candidate_id="exported-safe-candidate",
        heating_setpoint_c=20.5,
        cooling_setpoint_c=24.5,
        hold_minutes=60,
    )
    proposal = action(
        action_id="exported-action-1",
        run_id=run_id,
        observation_id=observed.observation_id,
        timestamp=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        action=applied,
    )
    validation = ValidationResult(
        run_id=run_id,
        observation_id=observed.observation_id,
        action_generation=1,
        timestamp=NOW + timedelta(minutes=5),
        accepted=True,
        proposed_action=applied,
        applied_action=applied,
        applied_expires_at=NOW + timedelta(hours=1),
    )
    application = store.apply_validated_action(
        proposal,
        validation,
        expected_run_id=run_id,
        timestamp=NOW + timedelta(minutes=5),
    )
    assert application.applied
    store.set_run_status(run_id, RunStatus.COMPLETED, timestamp=NOW + timedelta(hours=1))
    store.record_artifact(
        run_id,
        "energyplus_api_points",
        api_points,
        timestamp=NOW + timedelta(hours=1),
    )
    store.upsert_metric(
        run_id,
        "final_run_metrics",
        value_json={
            "source_artifacts": [
                str(api_points.resolve()),
                str((tmp_path / "external" / "eplusout.sql").resolve()),
            ],
            "status": "completed",
        },
        source="official-results-test",
        verified=True,
        timestamp=NOW + timedelta(hours=1),
    )

    output = export_run(database, runs_directory, run_id)

    with (output / "action_schedule.csv").open(encoding="utf-8", newline="") as handle:
        schedule = list(csv.DictReader(handle))
    assert len(schedule) == 1
    assert (
        datetime.fromisoformat(schedule[0].pop("simulation_timestamp").replace("Z", "+00:00"))
        == NOW
    )
    assert schedule == [
        {
            "observation_id": str(observed.observation_id),
            "action_generation": "1",
            "heating_setpoint_c": "20.5",
            "cooling_setpoint_c": "24.5",
            "hold_minutes": "60",
        }
    ]
    assert (output / "agent_replay.idf").read_bytes() == model.read_bytes()
    assert (output / "actuator_map.csv").read_bytes() == actuator_map.read_bytes()
    assert (output / "api_points.csv").read_bytes() == api_points.read_bytes()
    manifest = json.loads((output / "export-manifest.json").read_text(encoding="utf-8"))
    assert manifest["replay_action_count"] == 1
    assert manifest["replay_ready"] is True
    assert manifest["missing_controlled_artifacts"] == []
    assert manifest["energyplus_output_preserved"] is True
    metrics_json = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_json["final_run_metrics"]["value_json"]["source_artifacts"] == [
        "run/energyplus/api_points.csv",
        "external/eplusout.sql",
    ]
    with (output / "metrics.csv").open(encoding="utf-8", newline="") as handle:
        metric_rows = {row["metric_name"]: row for row in csv.DictReader(handle)}
    assert json.loads(metric_rows["final_run_metrics"]["value_json"])["source_artifacts"] == [
        "run/energyplus/api_points.csv",
        "external/eplusout.sql",
    ]
    host_root = str(tmp_path.resolve())
    for path in output.iterdir():
        if path.suffix.casefold() in {".csv", ".idf", ".json", ".jsonl"}:
            assert host_root not in path.read_text(encoding="utf-8")


def test_export_run_rejects_run_id_path_traversal_before_creating_output(
    tmp_path: Path,
) -> None:
    runs_directory = tmp_path / "runs"
    database = runs_directory / "ecoloop.db"
    SQLiteStore(database)

    with pytest.raises(RunStateError, match="safe path component"):
        export_run(database, runs_directory, "../escaped")

    assert not (tmp_path / "escaped").exists()


def test_source_zip_uses_release_allowlist_and_excludes_runtime_data(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "_git_clean_source_commit", lambda _root: "0" * 40)
    monkeypatch.setattr(
        reporting,
        "_SOURCE_ARCHIVE_REQUIRED_PATHS",
        reporting._SOURCE_ARCHIVE_REQUIRED_PATHS - {".gitattributes"},
    )
    archive_path = tmp_path / "ecoloop-source.zip"
    _write_source_zip(repository_root(), archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        ordered_names = archive.namelist()
        names = set(ordered_names)

    assert ordered_names == sorted(ordered_names)
    assert "README.md" in names
    assert "THIRD_PARTY_NOTICES.md" in names
    assert "models/generated/agent_replay.idf" in names
    assert "models/generated/preparation-manifest.json" in names
    assert not any(name.startswith(("runs/", "submission/", ".git/", ".venv/")) for name in names)
    assert not any(name.casefold().endswith((".epw", ".gguf", ".exe", ".key")) for name in names)
    assert not any(name.startswith(".env.") and name != ".env.example" for name in names)


def test_source_zip_git_mode_uses_tracked_and_exact_generated_paths(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "repository"
    root.mkdir()
    _run_git(root, "init", "--quiet")
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_REQUIRED_PATHS", frozenset({"README.md"}))

    _write_fixture(root, "README.md", "# Reviewed source\n")
    _write_fixture(root, "src/application.py", "VALUE = 1\n")
    _write_fixture(root, "runs/eplusout.sql")
    _write_fixture(root, "scratch/eplusout.sql")
    _write_fixture(root, "weather/default.epw")
    _write_fixture(root, "models/local.gguf")
    _write_fixture(root, "vendor/setup.exe")
    _write_fixture(root, "docs/archive.zip")
    _write_fixture(root, ".cache/payload.txt")
    _write_fixture(root, "config/credentials-prod.json", '{"value": "not-packaged"}\n')
    _write_fixture(root, "docs/unapproved.pptx")
    _run_git(root, "add", "--all")
    _commit_index(root)

    _write_fixture(root, "docs/unreviewed.md", "not reviewed\n")
    _write_fixture(root, "results/unreviewed.json", "{}\n")
    _write_fixture(root, "results/metrics.json", '{"facility_kwh": 42.0}\n')
    _write_minimal_presentation(root / "presentation" / "ecoloop-submission.pptx")

    first_archive = tmp_path / "first.zip"
    second_archive = tmp_path / "second.zip"
    _write_source_zip(root, first_archive)
    _write_source_zip(root, second_archive)

    assert first_archive.read_bytes() == second_archive.read_bytes()
    with zipfile.ZipFile(first_archive) as archive:
        names = archive.namelist()
    assert names == [
        "README.md",
        "presentation/ecoloop-submission.pptx",
        "results/metrics.json",
        "src/application.py",
    ]


def test_source_zip_matches_git_text_and_preserves_binary_and_untracked_bytes(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "repository"
    root.mkdir()
    _run_git(root, "init", "--quiet")
    monkeypatch.setattr(
        reporting,
        "_SOURCE_ARCHIVE_REQUIRED_PATHS",
        frozenset({".gitattributes", "README.md"}),
    )

    attributes = "* text=auto eol=lf\n*.png binary\n*.pptx binary\n"
    _write_fixture(root, ".gitattributes", attributes)
    _write_fixture(root, "README.md", "# Canonical source\n")
    _write_fixture(root, "src/application.py", "VALUE = 1\n")
    binary = b"\x89PNG\r\n\x1a\n\x00binary\r\npayload\r"
    binary_path = root / "assets" / "logo.png"
    binary_path.parent.mkdir(parents=True)
    binary_path.write_bytes(binary)
    _run_git(root, "add", "--all")
    _commit_index(root)

    metrics_path = root / "results" / "metrics.json"
    metrics_path.parent.mkdir(parents=True)
    metrics_path.write_bytes(b'{"verified": true}\r\n')
    presentation = root / "presentation" / "ecoloop-submission.pptx"
    _write_minimal_presentation(presentation)
    presentation_bytes = presentation.read_bytes()

    archive_path = tmp_path / "canonical.zip"
    source_commit = _write_source_zip(root, archive_path)

    assert source_commit == _run_git(root, "rev-parse", "HEAD").stdout.decode().strip()
    assert _run_git(root, "status", "--porcelain=v1", "--untracked-files=no").stdout == b""
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("README.md") == _run_git(root, "show", "HEAD:README.md").stdout
        assert (
            archive.read("src/application.py")
            == _run_git(root, "show", "HEAD:src/application.py").stdout
        )
        assert archive.read("assets/logo.png") == binary
        assert archive.read("results/metrics.json") == b'{"verified": true}\n'
        assert archive.read("presentation/ecoloop-submission.pptx") == presentation_bytes


@pytest.mark.parametrize("staged", [False, True])
def test_source_zip_blocks_uncommitted_tracked_worktree_or_index_changes(
    tmp_path,
    monkeypatch,
    staged,
):
    root = tmp_path / "repository"
    root.mkdir()
    _run_git(root, "init", "--quiet")
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_REQUIRED_PATHS", frozenset({"README.md"}))
    _write_fixture(root, "README.md", "# Committed source\n")
    _run_git(root, "add", "README.md")
    _commit_index(root)

    _write_fixture(root, "README.md", "# Dirty source\n")
    if staged:
        _run_git(root, "add", "README.md")

    archive_path = tmp_path / "blocked.zip"
    with pytest.raises(
        PackagingError,
        match="tracked source or index changes are uncommitted",
    ):
        _write_source_zip(root, archive_path)

    assert not archive_path.exists()


def test_source_zip_non_git_fallback_is_restricted(tmp_path, monkeypatch):
    root = tmp_path / "unpacked-source"
    root.mkdir()
    monkeypatch.setattr(reporting, "_git_tracked_files", lambda _root: None)
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_REQUIRED_PATHS", frozenset({"README.md"}))

    _write_fixture(root, "README.md", "# Unpacked release\n")
    _write_fixture(root, "src/application.py", "VALUE = 1\n")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_bytes(b"Safe documentation.\r\n")
    _write_fixture(root, "runs/eplusout.sql")
    _write_fixture(root, "weather/default.epw")
    _write_fixture(root, "config/.env.local", "PASS" + "WORD=not-packaged\n")
    _write_fixture(root, "results/unreviewed.json", "{}\n")
    _write_fixture(root, "results/comparison.json", '{"status": "verified"}\n')
    template = root / "presentation" / "template.pptx"
    completed_presentation = root / "presentation" / "ecoloop-submission.pptx"
    _write_minimal_presentation(template)
    _write_minimal_presentation(completed_presentation)
    completed_presentation_bytes = completed_presentation.read_bytes()

    archive_path = tmp_path / "fallback.zip"
    assert _write_source_zip(root, archive_path) is None

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert archive.read("docs/guide.md") == b"Safe documentation.\n"
        assert archive.read("presentation/ecoloop-submission.pptx") == completed_presentation_bytes
    assert {
        "README.md",
        "docs/guide.md",
        "presentation/ecoloop-submission.pptx",
        "presentation/template.pptx",
        "results/comparison.json",
        "src/application.py",
    } <= names
    assert "config/.env.local" not in names
    assert "results/unreviewed.json" not in names
    assert "runs/eplusout.sql" not in names
    assert "weather/default.epw" not in names


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ("ghp_" + ("A" * 36), "repository access token"),
        ("C:" + "/Us" + "ers/release-owner/project", "host-specific path"),
    ],
)
def test_source_zip_blocks_sensitive_tracked_content(
    tmp_path,
    monkeypatch,
    payload,
    expected_error,
):
    root = tmp_path / "repository"
    _write_fixture(root, "README.md", "# Source\n")
    _write_fixture(root, "docs/leak.txt", payload)
    monkeypatch.setattr(
        reporting,
        "_git_tracked_files",
        lambda _root: {
            Path("README.md"): "100644",
            Path("docs/leak.txt"): "100644",
        },
    )
    monkeypatch.setattr(reporting, "_git_clean_source_commit", lambda _root: "0" * 40)
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_REQUIRED_PATHS", frozenset({"README.md"}))
    archive_path = tmp_path / "blocked.zip"

    with pytest.raises(PackagingError, match=expected_error):
        _write_source_zip(root, archive_path)

    assert not archive_path.exists()


def test_source_zip_blocks_host_path_inside_generated_presentation(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    _write_fixture(root, "README.md", "# Source\n")
    presentation = root / "presentation" / "ecoloop-submission.pptx"
    _write_minimal_presentation(presentation, text=str(root / "private-result"))
    monkeypatch.setattr(
        reporting,
        "_git_tracked_files",
        lambda _root: {Path("README.md"): "100644"},
    )
    monkeypatch.setattr(reporting, "_git_clean_source_commit", lambda _root: "0" * 40)
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_REQUIRED_PATHS", frozenset({"README.md"}))

    with pytest.raises(PackagingError, match="host-specific path"):
        _write_source_zip(root, tmp_path / "blocked-presentation.zip")


def test_source_zip_excludes_git_symlink_and_oversized_file(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    _write_fixture(root, "README.md", "ok\n")
    _write_fixture(root, "src/link.py", "README.md\n")
    _write_fixture(root, "src/large.py", "x" * 33)
    monkeypatch.setattr(
        reporting,
        "_git_tracked_files",
        lambda _root: {
            Path("README.md"): "100644",
            Path("src/large.py"): "100644",
            Path("src/link.py"): "120000",
        },
    )
    monkeypatch.setattr(reporting, "_git_clean_source_commit", lambda _root: "0" * 40)
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_MAX_BYTES", 32)
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_REQUIRED_PATHS", frozenset({"README.md"}))

    archive_path = tmp_path / "safe.zip"
    _write_source_zip(root, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["README.md"]


def test_git_tracked_file_discovery_uses_fixed_non_shell_commands(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(str(root.resolve()) + "\n").encode(),
                stderr=b"",
            )
        record = f"100644 {'0' * 40} 0\tREADME.md\0".encode()
        return subprocess.CompletedProcess(command, 0, stdout=record, stderr=b"")

    monkeypatch.setattr(reporting.subprocess, "run", fake_run)

    assert reporting._git_tracked_files(root.resolve()) == {Path("README.md"): "100644"}
    assert [command for command, _ in calls] == [
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "ls-files", "--cached", "--stage", "-z"],
    ]
    assert all(kwargs["shell"] is False for _, kwargs in calls)


def test_git_checkout_does_not_fallback_when_git_enumeration_fails(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)

    def unavailable_git(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(reporting.subprocess, "run", unavailable_git)

    with pytest.raises(PackagingError, match="could not be enumerated safely"):
        reporting._git_tracked_files(root.resolve())


def test_nested_git_directory_does_not_use_non_git_fallback(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _run_git(checkout, "init", "--quiet")
    nested_source = checkout / "nested-source"
    nested_source.mkdir()

    with pytest.raises(PackagingError, match="top level"):
        reporting._git_tracked_files(nested_source.resolve())


def test_packaging_removes_only_known_stale_outputs(tmp_path):
    stale = tmp_path / "presentation.pdf"
    keep = tmp_path / "review-notes.txt"
    stale.write_bytes(b"old")
    keep.write_text("keep", encoding="utf-8")

    _clear_stale_submission_artifacts(tmp_path)

    assert not stale.exists()
    assert keep.read_text(encoding="utf-8") == "keep"
