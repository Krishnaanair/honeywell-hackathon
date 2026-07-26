"""Integration tests for honest run export and PDF packaging primitives."""

import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pypdf import PdfReader

import ecoloop.reporting as reporting
from ecoloop.config import repository_root
from ecoloop.db.store import SQLiteStore
from ecoloop.reporting import (
    PackagingError,
    _clear_stale_submission_artifacts,
    _write_source_zip,
    export_run,
    render_text_pdf,
)
from ecoloop.schemas import RunType


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


def test_source_zip_non_git_fallback_is_restricted(tmp_path, monkeypatch):
    root = tmp_path / "unpacked-source"
    root.mkdir()
    monkeypatch.setattr(reporting, "_git_tracked_files", lambda _root: None)
    monkeypatch.setattr(reporting, "_SOURCE_ARCHIVE_REQUIRED_PATHS", frozenset({"README.md"}))

    _write_fixture(root, "README.md", "# Unpacked release\n")
    _write_fixture(root, "src/application.py", "VALUE = 1\n")
    _write_fixture(root, "docs/guide.md", "Safe documentation.\n")
    _write_fixture(root, "runs/eplusout.sql")
    _write_fixture(root, "weather/default.epw")
    _write_fixture(root, "config/.env.local", "PASS" + "WORD=not-packaged\n")
    _write_fixture(root, "results/unreviewed.json", "{}\n")
    _write_fixture(root, "results/comparison.json", '{"status": "verified"}\n')
    _write_minimal_presentation(root / "presentation" / "template.pptx")
    _write_minimal_presentation(root / "presentation" / "ecoloop-submission.pptx")

    archive_path = tmp_path / "fallback.zip"
    _write_source_zip(root, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
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
