"""Versioned, checksum-verified SQLite migration runner."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ecoloop.time_utils import isoformat_utc, utc_now

MIGRATION_DIRECTORY: Final[Path] = Path(__file__).with_name("migrations")


class MigrationError(RuntimeError):
    """Raised when migration history is corrupt or cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One append-only SQL migration loaded from the package."""

    version: int
    name: str
    path: Path
    sql: str
    checksum: str


def discover_migrations(directory: Path = MIGRATION_DIRECTORY) -> tuple[Migration, ...]:
    """Load numbered ``*.sql`` migrations in strictly increasing order."""

    migrations: list[Migration] = []
    for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
        prefix, separator, name = path.stem.partition("_")
        if not separator or not prefix.isdigit() or not name:
            raise MigrationError(f"invalid migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise MigrationError(f"migration is empty: {path.name}")
        migrations.append(
            Migration(
                version=int(prefix),
                name=name,
                path=path,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    if not migrations:
        raise MigrationError(f"no SQL migrations found in {directory}")
    versions = [migration.version for migration in migrations]
    if versions != sorted(set(versions)):
        raise MigrationError("migration versions must be unique and increasing")
    return tuple(migrations)


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Apply pending migrations and verify previously applied checksums."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL DEFAULT '__schema__',
            timestamp TEXT NOT NULL,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL
        )
        """
    )
    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
    migrations = discover_migrations()
    available_versions = {migration.version for migration in migrations}
    unknown = set(applied) - available_versions
    if unknown:
        raise MigrationError(
            "database contains migration versions absent from this build: "
            + ", ".join(str(version) for version in sorted(unknown))
        )

    for migration in migrations:
        prior = applied.get(migration.version)
        if prior is not None:
            prior_name, prior_checksum = prior
            if prior_name != migration.name or prior_checksum != migration.checksum:
                raise MigrationError(f"applied migration {migration.version:03d} was modified")
            continue
        timestamp = isoformat_utc(utc_now()).replace("'", "''")
        escaped_name = migration.name.replace("'", "''")
        escaped_checksum = migration.checksum.replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migrations "
            "(version, run_id, timestamp, name, checksum) VALUES "
            f"({migration.version}, '__schema__', '{timestamp}', "
            f"'{escaped_name}', '{escaped_checksum}');\n"
            f"PRAGMA user_version = {migration.version};\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.rollback()
            raise MigrationError(f"failed to apply migration {migration.path.name}: {exc}") from exc

    return migrations[-1].version
