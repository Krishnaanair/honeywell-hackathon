"""Subprocess entry point for the explicitly fake MCP integration server."""

from __future__ import annotations

import argparse
from pathlib import Path

from ecoloop.mcp.path_policy import PathPolicy
from ecoloop.mcp.server import create_mcp_server
from tests.support.fake_mcp import FakeMCPService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Required acknowledgement that this server uses test-only fake telemetry.",
    )
    parser.add_argument(
        "--allowed-root",
        type=Path,
        help="Optional diagnostic path root selected by the test process.",
    )
    parser.add_argument(
        "--audit-file",
        type=Path,
        help="Optional JSONL audit sink selected by the test process.",
    )
    arguments = parser.parse_args()
    if not arguments.fake:
        parser.error("--fake is required")
    root = (arguments.allowed_root or Path.cwd()).resolve()
    if not root.is_dir():
        parser.error("--allowed-root must be an existing directory")
    audit_path = arguments.audit_file.resolve() if arguments.audit_file is not None else None
    if audit_path is not None and not audit_path.is_relative_to(root):
        parser.error("--audit-file must be inside --allowed-root")
    if audit_path is not None and audit_path.suffix.casefold() != ".jsonl":
        parser.error("--audit-file must use the .jsonl suffix")
    server = create_mcp_server(
        FakeMCPService(audit_path=audit_path),
        path_policy=PathPolicy(root),
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
