"""Run export, verified PDF generation, and submission packaging."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as ReportLabImage,
)

from ecoloop.config import Settings, repository_root
from ecoloop.exceptions import RunStateError


class PackagingError(RuntimeError):
    """Raised when a submission artifact cannot be generated honestly."""


_SOURCE_ARCHIVE_MAX_BYTES = 20 * 1024 * 1024
_SOURCE_ARCHIVE_GIT_TIMEOUT_SECONDS = 10.0
_SOURCE_ARCHIVE_FALLBACK_DIRECTORIES = (
    ".github",
    "config",
    "docs",
    "models",
    "scripts",
    "src",
    "tests",
    "weather",
)
_SOURCE_ARCHIVE_FALLBACK_FILES = (
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "Makefile",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "uv.lock",
)
_SOURCE_ARCHIVE_GENERATED_PATHS = frozenset(
    {
        "presentation/ecoloop-submission.pptx",
        "results/action_schedule.csv",
        "results/actuator_map.csv",
        "results/agent_replay.idf",
        "results/api_points.csv",
        "results/comparison.csv",
        "results/comparison.json",
        "results/decisions.jsonl",
        "results/metrics.csv",
        "results/metrics.json",
        "results/telemetry.csv",
    }
)
_SOURCE_ARCHIVE_PRESENTATION_PATHS = frozenset(
    {
        "presentation/ecoloop-submission.pptx",
        "presentation/template.pptx",
    }
)
_SOURCE_ARCHIVE_REQUIRED_PATHS = frozenset(
    {
        ".env.example",
        ".gitattributes",
        ".github/workflows/ci.yml",
        ".gitignore",
        "AGENTS.md",
        "LICENSE",
        "Makefile",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "config/default.toml",
        "docs/architecture.md",
        "docs/control-policy.md",
        "docs/demo-script.md",
        "docs/implementation-plan.md",
        "docs/limitations.md",
        "docs/methodology.md",
        "docs/presentation-content.md",
        "docs/progress.md",
        "docs/prompt-engineering.md",
        "docs/reproducibility.md",
        "docs/results.md",
        "docs/troubleshooting.md",
        "models/base/ENERGYPLUS_LICENSE.txt",
        "models/base/PROVENANCE.json",
        "models/base/SOURCE.md",
        "models/base/building.idf",
        "models/generated/action_schedule.csv",
        "models/generated/actuator_map.csv",
        "models/generated/agent_ready.idf",
        "models/generated/agent_replay.idf",
        "models/generated/baseline.idf",
        "models/generated/preparation-manifest.json",
        "pyproject.toml",
        "scripts/install_weather.py",
        "src/ecoloop/__init__.py",
        "tests/integration/test_mcp_stdio.py",
        "tests/unit/test_safety.py",
        "uv.lock",
        "weather/README.md",
        "weather/SOURCE.md",
    }
)
_SOURCE_ARCHIVE_EXCLUDED_ROOTS = frozenset({".git", "runs", "submission", "tmp"})
_SOURCE_ARCHIVE_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".aws",
        ".azure",
        ".cache",
        ".git",
        ".gnupg",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".ssh",
        ".tox",
        ".venv",
        ".vscode",
        ".virtualenv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "site-packages",
        "tmp",
        "venv",
    }
)
_SOURCE_ARCHIVE_EXCLUDED_FILES = frozenset(
    {
        ".coverage",
        ".ds_store",
        ".npmrc",
        ".pypirc",
        "desktop.ini",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "thumbs.db",
    }
)
_SOURCE_ARCHIVE_EXCLUDED_SUFFIXES = frozenset(
    {
        ".7z",
        ".apk",
        ".appx",
        ".bak",
        ".bin",
        ".bz2",
        ".class",
        ".ckpt",
        ".code-workspace",
        ".crt",
        ".db",
        ".deb",
        ".ddy",
        ".dmg",
        ".dll",
        ".docm",
        ".docx",
        ".dylib",
        ".epw",
        ".eso",
        ".exe",
        ".gguf",
        ".gz",
        ".h5",
        ".hdf5",
        ".iso",
        ".jar",
        ".joblib",
        ".key",
        ".kdbx",
        ".lib",
        ".msi",
        ".msix",
        ".mtr",
        ".npy",
        ".npz",
        ".o",
        ".obj",
        ".onnx",
        ".otf",
        ".p12",
        ".pem",
        ".pfx",
        ".pkg",
        ".pkl",
        ".pickle",
        ".pt",
        ".pth",
        ".pyc",
        ".pyd",
        ".pyo",
        ".rar",
        ".rpm",
        ".safetensors",
        ".so",
        ".sqlite",
        ".sqlite3",
        ".stat",
        ".swo",
        ".swp",
        ".tar",
        ".tbz2",
        ".tgz",
        ".tmp",
        ".ttf",
        ".war",
        ".whl",
        ".xlsm",
        ".xlsx",
        ".xz",
        ".zip",
        ".zst",
    }
)
_SOURCE_ARCHIVE_TEXT_SUFFIXES = frozenset(
    {
        "",
        ".cfg",
        ".csv",
        ".example",
        ".idf",
        ".ini",
        ".json",
        ".jsonl",
        ".lock",
        ".md",
        ".mmd",
        ".ps1",
        ".py",
        ".sh",
        ".sql",
        ".svg",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_SOURCE_ARCHIVE_SECRET_FILENAME = re.compile(
    r"(?i)^(?:"
    r"credentials?(?:[._-].*)?|"
    r"secrets?(?:[._-].*)?|"
    r"tokens?(?:[._-].*)?|"
    r"service[-_]?accounts?(?:[._-].*)?|"
    r".*private[-_]?keys?.*"
    r")$"
)
_SOURCE_ARCHIVE_ENERGYPLUS_OUTPUT = re.compile(
    r"(?i)^(?:eplus(?:out|mtr|ssz|zsz|tbl).*|readvars\.audit|sqlite\.err)$"
)
_SOURCE_ARCHIVE_USER_HOME = re.compile(
    r"(?i)(?:[a-z]:[\\/]+users[\\/]+|/(?:home|users)/)"
    r"(?P<username>[^\\/\s\"'<>]+)"
)
_SOURCE_ARCHIVE_PLACEHOLDER_USERS = frozenset(
    {
        "example",
        "sample",
        "user",
        "username",
        "your-user",
        "your_username",
    }
)
_SOURCE_ARCHIVE_SENSITIVE_CONTENT = (
    (
        "private-key material",
        re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----"),
    ),
    (
        "repository access token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"glpat-[A-Za-z0-9_-]{20,})\b"
        ),
    ),
    (
        "cloud access token",
        re.compile(
            r"\b(?:A(?:KI|SI)A[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,})\b"
        ),
    ),
    (
        "authorization bearer value",
        re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?bearer\s+[A-Za-z0-9._-]{12,}"),
    ),
    (
        "credential-bearing URL",
        re.compile(r"https?://[^/\s:@]+:[^@\s/]+@"),
    ),
    (
        "assigned credential value",
        re.compile(
            r"(?i)\b(?:api[-_]?key|access[-_]?token|password|client[-_]?secret|"
            r"private[-_]?key|aws[-_]?secret[-_]?access[-_]?key)\b"
            r"\s*[:=]\s*[\"']?[^\"'\s,;}]{8,}"
        ),
    ),
)
_SOURCE_ARCHIVE_PPTX_TEXT_ENTRY_MAX_BYTES = 5 * 1024 * 1024
_SOURCE_ARCHIVE_PPTX_TEXT_TOTAL_MAX_BYTES = 20 * 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest for a file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_run(
    database_path: Path,
    runs_dir: Path,
    run_id: str,
    *,
    output_dir: Path | None = None,
) -> Path:
    """Export one durable run without fabricating unavailable artifacts."""

    if (
        not run_id
        or run_id in {".", ".."}
        or "\x00" in run_id
        or "/" in run_id
        or "\\" in run_id
        or Path(run_id).name != run_id
    ):
        raise RunStateError("run_id must be one safe path component")
    database = database_path.expanduser().resolve()
    runs_root = runs_dir.expanduser().resolve()
    run_directory = runs_root / run_id
    destination = (
        output_dir.expanduser().resolve() if output_dir is not None else (run_directory / "export")
    )
    destination.mkdir(parents=True, exist_ok=True)
    with _connection(database) as connection:
        run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise RunStateError(f"unknown run_id: {run_id}")
        run_payload = dict(run)
        if bool(run_payload["is_fake"]):
            run_payload["data_status"] = "EXPLICIT_TEST_FAKE"
        else:
            run_payload["data_status"] = "REAL"
        _write_json(
            destination / "run.json",
            _portable_export_value(
                run_payload,
                runs_root=runs_root,
                run_directory=run_directory,
            ),
        )
        _export_table_csv(
            connection,
            destination / "telemetry.csv",
            "SELECT * FROM telemetry WHERE run_id = ? ORDER BY simulation_timestamp",
            (run_id,),
        )
        _export_table_csv(
            connection,
            destination / "zone_telemetry.csv",
            """
            SELECT * FROM zone_telemetry
            WHERE run_id = ?
            ORDER BY simulation_timestamp, zone_name
            """,
            (run_id,),
        )
        _export_jsonl(
            connection,
            destination / "decisions.jsonl",
            """
            SELECT * FROM agent_decisions
            WHERE run_id = ?
            ORDER BY timestamp, decision_id
            """,
            (run_id,),
        )
        action_rows = connection.execute(
            """
            SELECT observations.simulation_timestamp,
                   applied_actions.observation_id,
                   applied_actions.action_generation,
                   json_extract(applied_actions.applied_values_json, '$.heating_setpoint_c')
                       AS heating_setpoint_c,
                   json_extract(applied_actions.applied_values_json, '$.cooling_setpoint_c')
                       AS cooling_setpoint_c,
                   json_extract(applied_actions.applied_values_json, '$.hold_minutes')
                       AS hold_minutes
            FROM applied_actions
            JOIN observations
              ON observations.run_id = applied_actions.run_id
             AND observations.observation_id = applied_actions.observation_id
            WHERE applied_actions.run_id = ?
            ORDER BY observations.simulation_timestamp,
                     applied_actions.action_generation,
                     applied_actions.observation_id
            """,
            (run_id,),
        ).fetchall()
        _write_csv(
            destination / "action_schedule.csv",
            [dict(row) for row in action_rows],
            fieldnames=[
                "simulation_timestamp",
                "observation_id",
                "action_generation",
                "heating_setpoint_c",
                "cooling_setpoint_c",
                "hold_minutes",
            ],
        )
        metric_rows = connection.execute(
            """
            SELECT metric_name, value, value_json, units, source, verified, timestamp
            FROM metrics
            WHERE run_id = ?
            ORDER BY metric_name
            """,
            (run_id,),
        ).fetchall()
        portable_metric_rows: list[dict[str, Any]] = []
        portable_metric_values: dict[str, Any] = {}
        for row in metric_rows:
            row_payload = dict(row)
            parsed_value = _try_json(row_payload["value_json"])
            portable_value = _portable_export_value(
                parsed_value,
                runs_root=runs_root,
                run_directory=run_directory,
            )
            if row_payload["value_json"] is not None:
                row_payload["value_json"] = json.dumps(
                    portable_value,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            portable_metric_rows.append(row_payload)
            portable_metric_values[str(row["metric_name"])] = portable_value
        _write_csv(
            destination / "metrics.csv",
            portable_metric_rows,
            fieldnames=[
                "metric_name",
                "value",
                "value_json",
                "units",
                "source",
                "verified",
                "timestamp",
            ],
        )
        metrics_json = {
            str(row["metric_name"]): {
                "value": row["value"],
                "value_json": portable_metric_values[str(row["metric_name"])],
                "units": row["units"],
                "source": row["source"],
                "verified": bool(row["verified"]),
                "timestamp": row["timestamp"],
            }
            for row in metric_rows
        }
        _write_json(destination / "metrics.json", metrics_json)
        artifacts = connection.execute(
            """
            SELECT artifact_type, path, sha256, size_bytes, metadata_json
            FROM run_artifacts WHERE run_id = ? ORDER BY artifact_type, path
            """,
            (run_id,),
        ).fetchall()

    copied: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for artifact in artifacts:
        source = Path(str(artifact["path"])).expanduser()
        if not source.is_absolute():
            source = repository_root() / source
        source = source.resolve()
        if not source.is_file():
            missing.append(dict(artifact))
            continue
        if artifact["artifact_type"] in {
            "api_points",
            "energyplus_api_points",
            "actuator_map",
            "comparison_csv",
            "comparison_json",
            "replay_model",
        }:
            target_name = {
                "api_points": "api_points.csv",
                "energyplus_api_points": "api_points.csv",
                "actuator_map": "actuator_map.csv",
                "comparison_csv": "comparison.csv",
                "comparison_json": "comparison.json",
                "replay_model": "agent_replay.idf",
            }[str(artifact["artifact_type"])]
            target = destination / target_name
            if source != target.resolve():
                shutil.copy2(source, target)
            copied.append(
                {
                    "artifact_type": artifact["artifact_type"],
                    "source": str(source),
                    "export": str(target),
                    "sha256": sha256_file(target),
                }
            )

    model_source = _confined_run_input(
        run_payload.get("model_path"),
        runs_root=runs_root,
    )
    controlled_run = str(run_payload.get("run_type")) in {
        "agent",
        "fixed_override",
        "rule",
    }
    if controlled_run and model_source is not None:
        _copy_export_input(
            model_source,
            destination / "agent_replay.idf",
            artifact_type="replay_model_from_immutable_run_input",
            copied=copied,
        )
        _copy_export_input(
            model_source.parent / "actuator_map.csv",
            destination / "actuator_map.csv",
            artifact_type="actuator_map_from_immutable_run_input",
            copied=copied,
        )
    _copy_export_input(
        run_directory / "energyplus" / "api_points.csv",
        destination / "api_points.csv",
        artifact_type="energyplus_api_points",
        copied=copied,
    )

    expected_controlled_artifacts: tuple[str, ...] = ()
    if controlled_run and not bool(run_payload["is_fake"]):
        expected_controlled_artifacts = (
            "action_schedule.csv",
            "actuator_map.csv",
            "agent_replay.idf",
            "api_points.csv",
        )
    missing_controlled_artifacts = [
        name
        for name in expected_controlled_artifacts
        if not (destination / name).is_file()
        or (name == "action_schedule.csv" and len(action_rows) == 0)
    ]
    energyplus_output = run_directory / "energyplus"
    export_manifest = {
        "run_id": run_id,
        "exported_at": datetime.now(UTC).isoformat(),
        "data_status": run_payload["data_status"],
        "copied_artifacts": copied,
        "missing_recorded_artifacts": missing,
        "missing_controlled_artifacts": missing_controlled_artifacts,
        "replay_action_count": len(action_rows),
        "replay_ready": controlled_run
        and not bool(run_payload["is_fake"])
        and not missing_controlled_artifacts,
        "energyplus_output_directory": str(energyplus_output),
        "energyplus_output_preserved": energyplus_output.is_dir(),
    }
    _write_json(
        destination / "export-manifest.json",
        _portable_export_value(
            export_manifest,
            runs_root=runs_root,
            run_directory=run_directory,
        ),
    )
    return destination


def _portable_export_value(
    value: Any,
    *,
    runs_root: Path,
    run_directory: Path,
    field_name: str | None = None,
) -> Any:
    """Replace absolute path fields with stable repository/run references."""

    if isinstance(value, Mapping):
        return {
            str(key): _portable_export_value(
                item,
                runs_root=runs_root,
                run_directory=run_directory,
                field_name=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _portable_export_value(
                item,
                runs_root=runs_root,
                run_directory=run_directory,
                field_name=field_name,
            )
            for item in value
        ]
    if not isinstance(value, str) or not _path_bearing_field(field_name):
        return value
    return _portable_artifact_reference(
        value,
        runs_root=runs_root,
        run_directory=run_directory,
    )


def _path_bearing_field(field_name: str | None) -> bool:
    if field_name is None:
        return False
    folded = field_name.casefold()
    return folded in {"export", "source"} or any(
        marker in folded for marker in ("artifact", "directory", "file", "path")
    )


def _portable_artifact_reference(
    value: str,
    *,
    runs_root: Path,
    run_directory: Path,
) -> str:
    """Return a portable label for an absolute artifact path."""

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        windows_path = PureWindowsPath(value)
        if windows_path.is_absolute():
            return f"external/{windows_path.name}"
        return value
    resolved = candidate.resolve()
    roots = (
        ("run", run_directory.resolve()),
        ("repository", repository_root().resolve()),
        ("runs", runs_root.resolve()),
    )
    for label, root in roots:
        if resolved == root:
            return label
        if resolved.is_relative_to(root):
            return f"{label}/{resolved.relative_to(root).as_posix()}"
    return f"external/{resolved.name}"


def _confined_run_input(raw_path: Any, *, runs_root: Path) -> Path | None:
    """Resolve a recorded run input only below the repository or configured run root."""

    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = repository_root() / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    roots = (repository_root().resolve(), runs_root.resolve())
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        return None
    return resolved if resolved.is_file() else None


def _copy_export_input(
    source: Path,
    target: Path,
    *,
    artifact_type: str,
    copied: list[dict[str, Any]],
) -> bool:
    """Copy an exact run input when present without replacing an earlier artifact."""

    if not source.is_file():
        return target.is_file()
    target_resolved = target.resolve()
    if any(Path(str(item["export"])).resolve() == target_resolved for item in copied):
        return True
    source_resolved = source.resolve()
    if source_resolved != target_resolved:
        shutil.copy2(source_resolved, target_resolved)
    copied.append(
        {
            "artifact_type": artifact_type,
            "source": str(source_resolved),
            "export": str(target_resolved),
            "sha256": sha256_file(target_resolved),
        }
    )
    return True


def render_text_pdf(
    source_path: Path,
    output_path: Path,
    *,
    document_title: str,
) -> Path:
    """Render a Markdown-like source document as a polished, verified PDF."""

    source = source_path.expanduser().resolve()
    output = output_path.expanduser().resolve()
    if not source.is_file():
        raise PackagingError(f"PDF source is missing: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "EcoLoopBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#26352c"),
        spaceAfter=5,
    )
    title = ParagraphStyle(
        "EcoLoopTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=27,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#173c2b"),
        spaceAfter=14,
    )
    heading1 = ParagraphStyle(
        "EcoLoopH1",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=19,
        textColor=colors.HexColor("#176b45"),
        spaceBefore=10,
        spaceAfter=6,
    )
    heading2 = ParagraphStyle(
        "EcoLoopH2",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#245844"),
        spaceBefore=8,
        spaceAfter=4,
    )
    bullet = ParagraphStyle(
        "EcoLoopBullet",
        parent=normal,
        leftIndent=12,
        firstLineIndent=-7,
        bulletIndent=4,
        spaceAfter=3,
    )
    code = ParagraphStyle(
        "EcoLoopCode",
        parent=normal,
        fontName="Courier",
        fontSize=8,
        leading=10.5,
        backColor=colors.HexColor("#eef3ef"),
        borderPadding=5,
        spaceBefore=4,
        spaceAfter=6,
    )
    story: list[Any] = [Paragraph(_escape_pdf_text(document_title), title), Spacer(1, 4)]
    lines = source.read_text(encoding="utf-8").splitlines()
    in_code = False
    code_lines: list[str] = []
    table_lines: list[str] = []
    for raw_line in lines:
        line = _ascii_text(raw_line.rstrip())
        if line.startswith("```"):
            if in_code:
                story.append(Paragraph("<br/>".join(_escape_pdf_text(x) for x in code_lines), code))
                code_lines.clear()
                in_code = False
            else:
                _flush_table(story, table_lines, normal)
                in_code = True
            continue
        if in_code:
            code_lines.append(line or " ")
            continue
        if line.startswith("|") and line.endswith("|"):
            table_lines.append(line)
            continue
        _flush_table(story, table_lines, normal)
        if not line:
            story.append(Spacer(1, 3))
        elif image_path := _markdown_image_path(line, source.parent):
            image = ReportLabImage(str(image_path))
            scale = min(
                (174 * mm) / float(image.imageWidth),
                (110 * mm) / float(image.imageHeight),
                1.0,
            )
            image.drawWidth = float(image.imageWidth) * scale
            image.drawHeight = float(image.imageHeight) * scale
            image.hAlign = "CENTER"
            story.extend((Spacer(1, 4), image, Spacer(1, 8)))
        elif line.startswith("# "):
            # The PDF already has a document title.
            continue
        elif _is_publication_boundary_comment(line):
            continue
        elif line.startswith("## "):
            story.append(Paragraph(_inline_markup(line[3:]), heading1))
        elif line.startswith("### "):
            story.append(Paragraph(_inline_markup(line[4:]), heading2))
        elif line.startswith("- "):
            story.append(Paragraph(_inline_markup(line[2:]), bullet, bulletText="-"))
        elif _ordered_item(line):
            number, text = line.split(".", 1)
            story.append(Paragraph(_inline_markup(text.strip()), bullet, bulletText=f"{number}."))
        else:
            story.append(Paragraph(_inline_markup(line), normal))
    if code_lines:
        story.append(Paragraph("<br/>".join(_escape_pdf_text(x) for x in code_lines), code))
    _flush_table(story, table_lines, normal)

    document = BaseDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=document_title,
        author="EcoLoop Building Agents",
        subject="Hackathon submission evidence",
    )
    frame = Frame(
        document.leftMargin,
        document.bottomMargin,
        document.width,
        document.height,
        id="body",
    )
    document.addPageTemplates(
        [
            PageTemplate(
                id="EcoLoop",
                frames=[frame],
                onPage=lambda canvas, doc: _draw_page(canvas, doc, document_title),
            )
        ]
    )
    document.build(story)
    _verify_pdf(output, expected_text=document_title)
    return output


def package_submission(settings: Settings) -> dict[str, Any]:
    """Create the source ZIP, available PDFs, checksums, and manifest."""

    root = repository_root()
    submission = settings.resolved_submission_dir()
    submission.mkdir(parents=True, exist_ok=True)
    _clear_stale_submission_artifacts(submission)
    generated: list[Path] = []
    skipped: list[dict[str, str]] = []

    source_zip = submission / "ecoloop-source.zip"
    source_commit = _write_source_zip(root, source_zip)
    generated.append(source_zip)

    pdf_sources = (
        (
            root / "docs" / "architecture.md",
            submission / "system-architecture.pdf",
            "EcoLoop System Architecture",
        ),
        (
            root / "docs" / "results.md",
            submission / "results-report.pdf",
            "EcoLoop Results Report",
        ),
        (
            root / "docs" / "demo-script.md",
            submission / "demo-script.pdf",
            "EcoLoop Three-minute Demo Script",
        ),
    )
    for source, output, title in pdf_sources:
        generated.append(render_text_pdf(source, output, document_title=title))

    presentation = root / "presentation" / "ecoloop-submission.pptx"
    presentation_pdf = submission / "presentation.pdf"
    if presentation.is_file():
        renderer = _find_office_renderer()
        if renderer is None:
            skipped.append(
                {
                    "artifact": "presentation.pdf",
                    "reason": "PowerPoint or LibreOffice renderer not found",
                    "fix": (
                        "Install Microsoft PowerPoint or LibreOffice and ensure its "
                        "executable is discoverable, then rerun."
                    ),
                }
            )
        else:
            converted = _convert_presentation_pdf(renderer, presentation, submission)
            if converted != presentation_pdf:
                shutil.move(str(converted), presentation_pdf)
            _verify_pdf(presentation_pdf, expected_text="EcoLoop")
            generated.append(presentation_pdf)
    else:
        skipped.append(
            {
                "artifact": "presentation.pdf",
                "reason": "completed presentation PPTX is not available",
                "fix": "Generate presentation/ecoloop-submission.pptx and rerun.",
            }
        )

    checksum_path = submission / "checksums.txt"
    _write_checksums(generated, checksum_path)
    generated.append(checksum_path)
    manifest_path = submission / "submission-manifest.json"
    manifest = {
        "project": "EcoLoop Building Agents",
        "created_at": datetime.now(UTC).isoformat(),
        "source_commit": source_commit,
        "artifacts": [
            {
                "path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in generated
        ],
        "skipped": skipped,
        "integrity_policy": (
            "No fake or incomplete run metric is published as a production result."
        ),
    }
    _write_json(manifest_path, manifest)
    return manifest


def _clear_stale_submission_artifacts(submission: Path) -> None:
    """Remove only known generated outputs before rebuilding a submission."""

    for name in (
        "ecoloop-source.zip",
        "system-architecture.pdf",
        "results-report.pdf",
        "presentation.pdf",
        "demo-script.pdf",
        "checksums.txt",
        "submission-manifest.json",
    ):
        (submission / name).unlink(missing_ok=True)


def _write_source_zip(root: Path, output: Path) -> str | None:
    """Write a deterministic, reviewed source archive.

    A Git checkout contributes only paths recorded in its index. Exact result
    exports and the completed presentation may be added without tracking so a
    final evidence bundle can be assembled without committing run data. A
    source tree without its own Git metadata uses a restricted directory walk;
    this supports unpacked source distributions while retaining every archive
    safety check. Text payloads use canonical LF line endings, while binary
    payloads retain their exact bytes.

    Returns the verified source commit for a Git checkout, or ``None`` for the
    restricted non-Git fallback.
    """

    resolved_root = root.expanduser().resolve()
    resolved_output = output.expanduser().resolve()
    if not resolved_root.is_dir():
        raise PackagingError(f"source archive root is not a directory: {resolved_root}")
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.unlink(missing_ok=True)

    tracked = _git_tracked_files(resolved_root)
    source_commit = _git_clean_source_commit(resolved_root) if tracked is not None else None
    candidates = _source_archive_candidates(resolved_root, tracked)
    entries = _source_archive_entries(resolved_root, candidates)
    names = {name for name, _ in entries}
    missing = _SOURCE_ARCHIVE_REQUIRED_PATHS - names
    if missing:
        raise PackagingError(f"source ZIP is missing required files: {sorted(missing)}")

    try:
        with zipfile.ZipFile(
            resolved_output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, path in entries:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                mode = 0o100755 if path.suffix.casefold() == ".sh" else 0o100644
                info.external_attr = mode << 16
                archive.writestr(
                    info,
                    _source_archive_payload(path),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        if source_commit is not None:
            final_commit = _git_clean_source_commit(resolved_root)
            if final_commit != source_commit:
                raise PackagingError(
                    "Git HEAD changed while the source archive was being generated; "
                    "rerun packaging from a stable checkout."
                )
    except (OSError, RuntimeError, zipfile.BadZipFile):
        resolved_output.unlink(missing_ok=True)
        raise
    return source_commit


def _source_archive_candidates(
    root: Path,
    tracked: Mapping[Path, str] | None,
) -> dict[Path, str | None]:
    candidates: dict[Path, str | None] = (
        _fallback_source_archive_candidates(root) if tracked is None else dict(tracked)
    )
    for value in _SOURCE_ARCHIVE_GENERATED_PATHS:
        relative = Path(value)
        if (root / relative).exists():
            candidates.setdefault(relative, None)
    return candidates


def _git_tracked_files(root: Path) -> dict[Path, str] | None:
    """Return Git-indexed paths and modes, or ``None`` outside a Git checkout."""

    has_git_metadata = any(
        (directory / ".git").exists() or (directory / ".git").is_symlink()
        for directory in (root, *root.parents)
    )
    commands = (
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "ls-files", "--cached", "--stage", "-z"],
    )
    completed: list[subprocess.CompletedProcess[bytes]] = []
    for command in commands:
        try:
            result = subprocess.run(  # noqa: S603
                command,
                cwd=root,
                check=False,
                capture_output=True,
                shell=False,
                timeout=_SOURCE_ARCHIVE_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if has_git_metadata:
                raise PackagingError(
                    "Git metadata exists, but tracked source files could not be enumerated safely."
                ) from exc
            return None
        completed.append(result)
        if result.returncode != 0:
            if has_git_metadata:
                raise PackagingError(
                    "Git metadata exists, but tracked source files could not be enumerated safely."
                )
            return None

    top_level_bytes = completed[0].stdout.rstrip(b"\r\n")
    top_level = Path(os.fsdecode(top_level_bytes)).expanduser().resolve()
    if top_level != root:
        raise PackagingError("source archive root must be the top level of its Git checkout")

    tracked: dict[Path, str] = {}
    for record in completed[1].stdout.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise PackagingError("Git returned a malformed tracked-file record.")
        mode = fields[0].decode("ascii", errors="strict")
        stage = fields[2].decode("ascii", errors="strict")
        if stage != "0":
            raise PackagingError("Git has unresolved index entries; source packaging is blocked.")
        relative = Path(os.fsdecode(raw_path))
        tracked[relative] = mode
    return tracked


def _git_clean_source_commit(root: Path) -> str:
    """Return ``HEAD`` only when tracked worktree and index content are clean."""

    commands = (
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"],
        ["git", "rev-parse", "--verify", "HEAD"],
    )
    completed: list[subprocess.CompletedProcess[bytes]] = []
    for command in commands:
        try:
            result = subprocess.run(  # noqa: S603
                command,
                cwd=root,
                check=False,
                capture_output=True,
                shell=False,
                timeout=_SOURCE_ARCHIVE_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PackagingError(
                "Git metadata exists, but source provenance could not be verified safely."
            ) from exc
        if result.returncode != 0:
            raise PackagingError(
                "Git metadata exists, but source provenance could not be verified safely."
            )
        completed.append(result)

    if completed[0].stdout:
        raise PackagingError(
            "source packaging blocked: tracked source or index changes are uncommitted"
        )
    try:
        commit = completed[1].stdout.strip().decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise PackagingError("Git returned a malformed source commit.") from exc
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) is None:
        raise PackagingError("Git returned a malformed source commit.")
    return commit.lower()


def _fallback_source_archive_candidates(root: Path) -> dict[Path, str | None]:
    """Collect a constrained source set when no checkout metadata exists."""

    candidates: dict[Path, str | None] = {}
    for value in _SOURCE_ARCHIVE_FALLBACK_FILES:
        relative = Path(value)
        if (root / relative).exists():
            candidates[relative] = None
    for directory_name in _SOURCE_ARCHIVE_FALLBACK_DIRECTORIES:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            candidates[path.relative_to(root)] = None
    template = Path("presentation/template.pptx")
    if (root / template).exists():
        candidates[template] = None
    return candidates


def _source_archive_entries(
    root: Path,
    candidates: Mapping[Path, str | None],
) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    casefolded_names: dict[str, str] = {}
    for relative, git_mode in sorted(candidates.items(), key=lambda item: item[0].as_posix()):
        violation = _source_archive_path_violation(root, relative, git_mode)
        if violation is not None:
            continue
        path = root / relative
        name = relative.as_posix()
        content_violation = _source_archive_content_violation(path, root)
        if content_violation is not None:
            raise PackagingError(
                f"source packaging blocked for {name}: {content_violation}; "
                "remove or sanitize the file"
            )
        folded = name.casefold()
        existing = casefolded_names.get(folded)
        if existing is not None and existing != name:
            raise PackagingError(
                f"source packaging has case-conflicting paths: {existing!r} and {name!r}"
            )
        casefolded_names[folded] = name
        entries.append((name, path))
    return entries


def _source_archive_path_violation(
    root: Path,
    relative: Path,
    git_mode: str | None,
) -> str | None:
    if git_mode == "120000":
        return "Git symlink"
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        return "unsafe path"
    name = relative.as_posix()
    if not name or "\\" in name or ":" in relative.parts[0]:
        return "unsafe path"
    folded_parts = tuple(part.casefold() for part in relative.parts)
    if folded_parts[0] in _SOURCE_ARCHIVE_EXCLUDED_ROOTS:
        return "runtime or package output"
    if any(
        part in _SOURCE_ARCHIVE_EXCLUDED_DIRECTORIES or part.endswith((".dist-info", ".egg-info"))
        for part in folded_parts
    ):
        return "cache or environment directory"
    if folded_parts[0] == "results" and name not in _SOURCE_ARCHIVE_GENERATED_PATHS:
        return "unapproved result artifact"

    path = root / relative
    folded_filename = path.name.casefold()
    if folded_filename in _SOURCE_ARCHIVE_EXCLUDED_FILES:
        return "sensitive local file"
    if folded_filename.startswith(".env") and name != ".env.example":
        return "local environment file"
    if _SOURCE_ARCHIVE_SECRET_FILENAME.fullmatch(path.name):
        return "credential-like filename"
    if _SOURCE_ARCHIVE_ENERGYPLUS_OUTPUT.fullmatch(path.name):
        return "raw EnergyPlus output"
    suffix = path.suffix.casefold()
    if suffix == ".pptx" and name not in _SOURCE_ARCHIVE_PRESENTATION_PATHS:
        return "unapproved presentation archive"
    if suffix in _SOURCE_ARCHIVE_EXCLUDED_SUFFIXES:
        return "binary, weather, model, installer, or archive suffix"
    if path.is_symlink():
        return "filesystem symlink"

    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return "filesystem symlink"
    if not path.is_file():
        return "not a regular file"
    try:
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(root)
        size = path.stat().st_size
    except (OSError, ValueError):
        return "unreadable or escaped path"
    if size > _SOURCE_ARCHIVE_MAX_BYTES:
        return "oversized file"
    return None


def _source_archive_content_violation(path: Path, root: Path) -> str | None:
    fragments, read_violation = _source_archive_text_fragments(path)
    if read_violation is not None:
        return read_violation
    for text in fragments:
        for label, pattern in _SOURCE_ARCHIVE_SENSITIVE_CONTENT:
            if pattern.search(text):
                return label
        if _contains_host_specific_path(text, root):
            return "host-specific path"
    return None


def _source_archive_payload(path: Path) -> bytes:
    """Read one archive payload with canonical line endings for approved text."""

    data = path.read_bytes()
    if path.suffix.casefold() in _SOURCE_ARCHIVE_TEXT_SUFFIXES:
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def _source_archive_text_fragments(path: Path) -> tuple[tuple[str, ...], str | None]:
    if path.suffix.casefold() == ".pptx":
        return _presentation_text_fragments(path)
    try:
        data = path.read_bytes()
    except OSError:
        return (), "unreadable file"
    if path.suffix.casefold() not in _SOURCE_ARCHIVE_TEXT_SUFFIXES and b"\0" in data[:8192]:
        return (data.decode("latin-1", errors="ignore"),), None
    return (data.decode("utf-8", errors="replace"),), None


def _presentation_text_fragments(path: Path) -> tuple[tuple[str, ...], str | None]:
    fragments: list[str] = []
    total_size = 0
    try:
        with zipfile.ZipFile(path) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                is_text_entry = info.filename.casefold().endswith((".xml", ".rels", ".txt"))
                if info.is_dir() or not is_text_entry:
                    continue
                if info.file_size > _SOURCE_ARCHIVE_PPTX_TEXT_ENTRY_MAX_BYTES:
                    return (), "oversized presentation metadata"
                total_size += info.file_size
                if total_size > _SOURCE_ARCHIVE_PPTX_TEXT_TOTAL_MAX_BYTES:
                    return (), "oversized presentation metadata"
                fragments.append(archive.read(info).decode("utf-8", errors="replace"))
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        return (), "invalid or unreadable presentation archive"
    return tuple(fragments), None


def _contains_host_specific_path(text: str, root: Path) -> bool:
    folded_text = text.casefold()
    root_values = {
        str(root),
        root.as_posix(),
        str(root).replace("\\", "\\\\"),
    }
    if any(len(value) > 3 and value.casefold() in folded_text for value in root_values):
        return True
    for match in _SOURCE_ARCHIVE_USER_HOME.finditer(text):
        username = match.group("username").casefold()
        if username not in _SOURCE_ARCHIVE_PLACEHOLDER_USERS:
            return True
    return False


@contextmanager
def _connection(database: Path) -> Iterator[sqlite3.Connection]:
    if not database.is_file():
        raise RunStateError(f"run database does not exist: {database}")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def _export_table_csv(
    connection: sqlite3.Connection,
    output: Path,
    query: str,
    parameters: Sequence[Any],
) -> None:
    cursor = connection.execute(query, parameters)
    rows = cursor.fetchall()
    fieldnames = [str(item[0]) for item in cursor.description]
    _write_csv(output, [dict(row) for row in rows], fieldnames=fieldnames)


def _export_jsonl(
    connection: sqlite3.Connection,
    output: Path,
    query: str,
    parameters: Sequence[Any],
) -> None:
    rows = connection.execute(query, parameters).fetchall()
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=True, sort_keys=True))
            handle.write("\n")


def _write_csv(
    output: Path, rows: Iterable[Mapping[str, Any]], *, fieldnames: Sequence[str]
) -> None:
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _try_json(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)


def _ascii_text(text: str) -> str:
    replacements = {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2192": "->",
        "\u2190": "<-",
        "\u2264": "<=",
        "\u2265": ">=",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _escape_pdf_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_markup(text: str) -> str:
    """Convert bounded Markdown inline spans into trusted ReportLab markup.

    Only paired ``**bold**`` and backtick code spans are recognized. All source
    text is escaped before the renderer-generated tags are inserted, so source
    HTML is displayed literally rather than interpreted.
    """

    parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        bold_start = text.find("**", cursor)
        code_start = text.find("`", cursor)
        link_match = re.search(r"\[([^\]\n]+)]\(([^)\n]+)\)", text[cursor:])
        link_start = cursor + link_match.start() if link_match is not None else -1
        starts = [
            (index, marker)
            for index, marker in (
                (bold_start, "**"),
                (code_start, "`"),
                (link_start, "link"),
            )
            if index >= 0
        ]
        if not starts:
            parts.append(_escape_pdf_text(text[cursor:]))
            break
        start, marker = min(starts, key=lambda item: item[0])
        parts.append(_escape_pdf_text(text[cursor:start]))
        if marker == "link":
            if link_match is None:
                raise AssertionError("link start was recorded without a match")
            label = link_match.group(1)
            target = link_match.group(2).strip()
            label_markup = _inline_markup(label)
            if re.fullmatch(r"https?://[^\s<>]+", target, flags=re.IGNORECASE):
                safe_target = _escape_pdf_attribute(target)
                parts.append(f'<a href="{safe_target}" color="#176b45">{label_markup}</a>')
            else:
                parts.append(label_markup)
            cursor += link_match.end()
            continue
        end = text.find(marker, start + len(marker))
        if end < 0:
            parts.append(_escape_pdf_text(text[start:]))
            break
        content = text[start + len(marker) : end]
        if not content:
            parts.append(_escape_pdf_text(marker))
            cursor = start + len(marker)
            continue
        escaped_content = _escape_pdf_text(content)
        if marker == "**":
            parts.append(f"<b>{escaped_content}</b>")
        else:
            parts.append(f'<font name="Courier">{escaped_content}</font>')
        cursor = end + len(marker)
    return "".join(parts)


def _escape_pdf_attribute(text: str) -> str:
    """Escape a validated value before inserting it into renderer markup."""

    return _escape_pdf_text(text).replace('"', "&quot;").replace("'", "&#39;")


def _is_publication_boundary_comment(line: str) -> bool:
    """Return whether a line is one of the controlled publication markers."""

    return bool(
        re.fullmatch(
            r"<!--\s*(?:BEGIN|END)\s+VERIFIED_EVALUATION_BLOCK\s*-->",
            line.strip(),
            flags=re.IGNORECASE,
        )
    )


def _ordered_item(line: str) -> bool:
    prefix, dot, remainder = line.partition(".")
    return bool(dot and prefix.isdigit() and remainder.startswith(" "))


def _markdown_image_path(line: str, source_directory: Path) -> Path | None:
    match = re.fullmatch(r"!\[[^\]]*]\(([^)]+)\)", line.strip())
    if match is None:
        return None
    raw_path = match.group(1).strip()
    if "://" in raw_path:
        return None
    candidate = (source_directory / raw_path).resolve()
    root = repository_root().resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise PackagingError(f"Markdown image is missing or outside the repository: {raw_path}")
    return candidate


def _flush_table(story: list[Any], lines: list[str], style: ParagraphStyle) -> None:
    if not lines:
        return
    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in lines
        if set(line.replace("|", "").replace(":", "").replace("-", "").strip())
    ]
    lines.clear()
    if not rows:
        return
    column_widths = _table_column_widths(rows)
    header_style = ParagraphStyle(
        "EcoLoopTableHeader",
        parent=style,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#173c2b"),
    )
    rendered_rows = [
        [
            Paragraph(
                _inline_markup(cell),
                header_style if row_index == 0 else style,
            )
            for cell in row
        ]
        for row_index, row in enumerate(rows)
    ]
    table = Table(
        rendered_rows,
        colWidths=column_widths,
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfeee4")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#173c2b")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#a8b8ae")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(KeepTogether(table))
    story.append(Spacer(1, 6))


def _table_column_widths(rows: Sequence[Sequence[str]]) -> list[float]:
    """Return stable widths for the bounded report table schemas."""

    column_count = len(rows[0]) if rows else 0
    if column_count == 0:
        return []
    headers = [re.sub(r"[*`]", "", cell).strip().casefold() for cell in rows[0]]
    ratios: tuple[float, ...]
    if column_count == 4 and headers[0] == "metric":
        ratios = (0.36, 0.18, 0.18, 0.28)
    elif column_count == 6 and headers[0] == "evidence run":
        ratios = (0.24, 0.16, 0.16, 0.14, 0.15, 0.15)
    else:
        ratios = (1 / column_count,) * column_count
    return [174 * mm * ratio for ratio in ratios]


def _draw_page(canvas: Any, document: Any, title: str) -> None:
    canvas.saveState()
    width, _ = A4
    canvas.setStrokeColor(colors.HexColor("#8bb89e"))
    canvas.setLineWidth(0.7)
    canvas.line(18 * mm, 12 * mm, width - 18 * mm, 12 * mm)
    canvas.setFillColor(colors.HexColor("#617069"))
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(18 * mm, 7.5 * mm, _ascii_text(title))
    canvas.drawRightString(width - 18 * mm, 7.5 * mm, f"Page {document.page}")
    canvas.restoreState()


def _verify_pdf(path: Path, *, expected_text: str) -> None:
    reader = PdfReader(str(path))
    if not reader.pages:
        raise PackagingError(f"generated PDF has no pages: {path}")
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    if expected_text.casefold() not in extracted.casefold():
        raise PackagingError(f"generated PDF is missing expected text: {expected_text}")


def _find_office_renderer() -> Path | None:
    for name in ("soffice", "libreoffice"):
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved)
    common = (
        Path("C:/Program Files/LibreOffice/program/soffice.exe"),
        Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
        Path("C:/Program Files/Microsoft Office/root/Office16/POWERPNT.EXE"),
        Path("C:/Program Files (x86)/Microsoft Office/root/Office16/POWERPNT.EXE"),
    )
    return next((path for path in common if path.is_file()), None)


def _convert_presentation_pdf(renderer: Path, presentation: Path, output_dir: Path) -> Path:
    if renderer.name.casefold() == "powerpnt.exe":
        return _convert_with_powerpoint(presentation, output_dir)
    command = [
        str(renderer),
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(presentation),
    ]
    # The executable is discovered locally and all arguments are repository-owned paths.
    completed = subprocess.run(  # noqa: S603
        command, check=False, capture_output=True, text=True, timeout=180
    )
    if completed.returncode != 0:
        raise PackagingError(
            "presentation PDF conversion failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    output = output_dir / f"{presentation.stem}.pdf"
    if not output.is_file():
        raise PackagingError("presentation renderer exited without a PDF")
    return output


def _convert_with_powerpoint(presentation: Path, output_dir: Path) -> Path:
    script = repository_root() / "scripts" / "export_presentation_pdf.ps1"
    powershell = (
        Path(
            os.environ.get(
                "SYSTEMROOT",
                "C:/Windows",
            )
        )
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not script.is_file() or not powershell.is_file():
        raise PackagingError(
            "PowerPoint is installed, but the verified PowerShell export pipeline is missing."
        )
    output = output_dir / f"{presentation.stem}.pdf"
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-InputPath",
        str(presentation),
        "-OutputPath",
        str(output),
    ]
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise PackagingError(
            "PowerPoint PDF conversion failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    if not output.is_file():
        raise PackagingError("PowerPoint exited without a PDF")
    return output


def _write_checksums(paths: Sequence[Path], output: Path) -> None:
    lines = [f"{sha256_file(path)}  {path.name}" for path in sorted(paths)]
    output.write_text("\n".join(lines) + "\n", encoding="ascii")
