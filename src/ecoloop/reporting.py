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
from pathlib import Path
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

    database = database_path.expanduser().resolve()
    destination = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else (runs_dir.expanduser().resolve() / run_id / "export")
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
        _write_json(destination / "run.json", run_payload)
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
        _export_table_csv(
            connection,
            destination / "action_schedule.csv",
            """
            SELECT timestamp, observation_id, action_generation,
                   json_extract(applied_values_json, '$.heating_setpoint_c')
                       AS heating_setpoint_c,
                   json_extract(applied_values_json, '$.cooling_setpoint_c')
                       AS cooling_setpoint_c,
                   expiry, fallback_status, reason_code
            FROM applied_actions
            WHERE run_id = ?
            ORDER BY action_generation
            """,
            (run_id,),
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
        _write_csv(
            destination / "metrics.csv",
            [dict(row) for row in metric_rows],
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
                "value_json": _try_json(row["value_json"]),
                "units": row["units"],
                "source": row["source"],
                "verified": bool(row["verified"]),
                "timestamp": row["timestamp"],
            }
            for row in metric_rows
        }
        _write_json(destination / "metrics.json", metrics_json)
        _export_table_csv(
            connection,
            destination / "actuator_map.csv",
            """
            SELECT artifact_type, path, sha256, metadata_json
            FROM run_artifacts
            WHERE run_id = ? AND artifact_type IN ('actuator_map', 'handle_registry')
            ORDER BY artifact_type, path
            """,
            (run_id,),
        )
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
            "actuator_map",
            "comparison_csv",
            "comparison_json",
            "replay_model",
            "action_schedule",
        }:
            target_name = {
                "api_points": "api_points.csv",
                "actuator_map": "actuator_map.csv",
                "comparison_csv": "comparison.csv",
                "comparison_json": "comparison.json",
                "replay_model": "agent_replay.idf",
                "action_schedule": "action_schedule.csv",
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
    _write_json(
        destination / "export-manifest.json",
        {
            "run_id": run_id,
            "exported_at": datetime.now(UTC).isoformat(),
            "data_status": run_payload["data_status"],
            "copied_artifacts": copied,
            "missing_recorded_artifacts": missing,
        },
    )
    return destination


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
        elif line.startswith("## "):
            story.append(Paragraph(_escape_pdf_text(line[3:]), heading1))
        elif line.startswith("### "):
            story.append(Paragraph(_escape_pdf_text(line[4:]), heading2))
        elif line.startswith("- "):
            story.append(Paragraph(_escape_pdf_text(line[2:]), bullet, bulletText="-"))
        elif _ordered_item(line):
            number, text = line.split(".", 1)
            story.append(Paragraph(_escape_pdf_text(text.strip()), bullet, bulletText=f"{number}."))
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
    _write_source_zip(root, source_zip)
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
        "source_commit": _git_commit(root),
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


def _write_source_zip(root: Path, output: Path) -> None:
    source_directories = (
        ".github",
        "config",
        "docs",
        "models",
        "presentation",
        "results",
        "scripts",
        "src",
        "tests",
        "weather",
    )
    source_files = (
        ".env.example",
        ".gitignore",
        "AGENTS.md",
        "LICENSE",
        "Makefile",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "uv.lock",
    )
    excluded_directories = {
        "__pycache__",
        ".cache",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".vscode",
        "build",
        "dist",
        "htmlcov",
        "tmp",
        "venv",
    }
    excluded_files = {
        ".coverage",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "eplusout.eso",
        "eplusout.mtr",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
    }
    excluded_suffixes = {
        ".7z",
        ".bin",
        ".crt",
        ".ddy",
        ".dmg",
        ".dll",
        ".epw",
        ".eso",
        ".exe",
        ".gguf",
        ".gz",
        ".key",
        ".kdbx",
        ".msi",
        ".mtr",
        ".onnx",
        ".p12",
        ".pem",
        ".pfx",
        ".pkg",
        ".pt",
        ".pth",
        ".safetensors",
        ".so",
        ".stat",
        ".tar",
        ".tgz",
        ".xz",
        ".zip",
    }
    candidates = [root / name for name in source_files]
    for directory_name in source_directories:
        directory = root / directory_name
        if directory.is_dir():
            candidates.extend(directory.rglob("*"))

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(candidates)):
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in excluded_directories for part in relative.parts):
                continue
            if path.name in excluded_files:
                continue
            if path.name.startswith(".env") and path.name != ".env.example":
                continue
            if path.suffix.casefold() in excluded_suffixes:
                continue
            if path.stat().st_size > 20 * 1024 * 1024:
                continue
            archive.write(path, relative.as_posix())
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    required = {
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        ".env.example",
        ".gitignore",
        ".github/workflows/ci.yml",
        "AGENTS.md",
        "Makefile",
        "config/default.toml",
        "scripts/install_weather.py",
        "src/ecoloop/__init__.py",
        "tests/unit/test_safety.py",
        "tests/integration/test_mcp_stdio.py",
        "models/base/building.idf",
        "models/base/ENERGYPLUS_LICENSE.txt",
        "models/base/PROVENANCE.json",
        "models/base/SOURCE.md",
        "models/generated/baseline.idf",
        "models/generated/agent_ready.idf",
        "models/generated/agent_replay.idf",
        "models/generated/action_schedule.csv",
        "models/generated/actuator_map.csv",
        "models/generated/preparation-manifest.json",
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
        "weather/README.md",
        "weather/SOURCE.md",
    }
    missing = required - names
    if missing:
        raise PackagingError(f"source ZIP is missing required files: {sorted(missing)}")


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
    escaped = _escape_pdf_text(text)
    while "**" in escaped:
        escaped = escaped.replace("**", "<b>", 1)
        if "**" in escaped:
            escaped = escaped.replace("**", "</b>", 1)
        else:
            escaped = escaped.replace("<b>", "**", 1)
            break
    return escaped


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
        [_escape_pdf_text(cell.strip()) for cell in line.strip("|").split("|")]
        for line in lines
        if set(line.replace("|", "").replace(":", "").replace("-", "").strip())
    ]
    lines.clear()
    if not rows:
        return
    width = 174 * mm / max(len(rows[0]), 1)
    table = Table(
        [[Paragraph(cell, style) for cell in row] for row in rows],
        colWidths=[width] * len(rows[0]),
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


def _git_commit(root: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    completed = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None
