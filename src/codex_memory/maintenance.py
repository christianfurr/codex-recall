"""Private, local maintenance commands for the memory database."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Any, Sequence
from urllib.parse import quote
from uuid import uuid4

from .database import MemoryStore, database_lock, default_database_path
from .security import secure_directory


def _path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def _safe_parent(path: Path, *, create: bool = False) -> None:
    """Reject symlink components; create new directories privately."""
    ancestors = list(reversed(path.parent.parents)) + [path.parent]
    for directory in ancestors:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if not create:
                raise ValueError("The parent directory does not exist.") from None
            directory.mkdir(mode=0o700)
            info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("A path component is not a real directory.")
    info = path.parent.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("Use a directory owned by you without group/world write access.")


def _safe_file(path: Path, *, required: bool = True) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError("The requested file does not exist.") from None
        return False
    if info.st_nlink == 0 and not required:
        # A concurrently closed SQLite connection may unlink a sidecar while
        # lstat is returning its metadata.
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Only regular files without symbolic or hard links are accepted.")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("The file must be owned by you and have private permissions (0600).")
    return True


def _check_sidecars(path: Path, *, reject: bool = False) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if _safe_file(sidecar, required=False) and reject:
            raise ValueError("Restore requires a standalone SQLite backup without sidecar files.")


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(
            "file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=5
        )
    else:
        connection = sqlite3.connect(str(path), timeout=5)
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _temp_file(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    return Path(name)


def _sync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_new(temporary: Path, destination: Path) -> None:
    """Atomically publish without overwriting an existing backup/export."""
    _sync_file(temporary)
    os.link(temporary, destination)
    temporary.unlink()
    _sync_directory(destination.parent)


def _cleanup_temporary(path: Path) -> None:
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _validate(connection: sqlite3.Connection, *, fts: bool = False) -> None:
    """Check a supported snapshot before it can replace durable data."""
    from .migrations import SCHEMA_VERSION, migrate

    def manifest(candidate: sqlite3.Connection) -> dict[str, tuple[str, str, str | None]]:
        return {
            row[0]: (row[1], row[2], row[3])
            for row in candidate.execute(
                "SELECT name, type, tbl_name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        }

    with closing(sqlite3.connect(":memory:")) as reference:
        migrate(reference)
        if manifest(connection) != manifest(reference):
            raise ValueError("Backup schema does not exactly match this application version.")
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise ValueError("SQLite integrity validation failed.")
    versions = connection.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    if versions != [(SCHEMA_VERSION,)]:
        raise ValueError("Backup schema version is unsupported; use the matching application version.")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
    required = {
        "id", "content", "category", "scope", "project", "source", "created_at", "updated_at",
        "expires_at", "superseded_by", "status", "duplicate_key",
    }
    if columns != required:
        raise ValueError("Backup memory columns do not match the supported schema.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Backup contains broken memory references.")
    if fts:
        connection.execute("INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)")
        connection.commit()


def _snapshot(source: Path, destination: Path, *, validate: bool = True) -> None:
    """Use SQLite's live backup API and produce a standalone, validated file."""
    stalled_since: float | None = None

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal stalled_since
        # SQLite's stable BUSY/LOCKED codes; named constants are available only
        # in newer Python sqlite3 modules, while Python 3.10 is supported.
        if status in (5, 6):
            stalled_since = stalled_since or time.monotonic()
            if time.monotonic() - stalled_since >= 10.0:
                raise TimeoutError("SQLite backup is busy. Stop other SQLite clients and retry.")
        else:
            stalled_since = None

    with closing(_connect(source, readonly=True)) as incoming:
        with closing(_connect(destination)) as outgoing:
            incoming.backup(outgoing, pages=256, sleep=0.05, progress=progress)
            outgoing.execute("PRAGMA journal_mode=DELETE")
            if validate:
                _validate(outgoing, fts=True)
    _safe_file(destination)
    _check_sidecars(destination, reject=True)


def backup_database(database: str | Path, destination: str | Path) -> dict[str, Any]:
    database, destination = _path(database), _path(destination)
    _safe_parent(database)
    _safe_file(database)
    _safe_parent(destination, create=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists; choose a new backup filename.")
    temporary = _temp_file(destination)
    try:
        with database_lock(database):
            _safe_parent(database)
            _safe_file(database)
            _check_sidecars(database)
            _snapshot(database, temporary)
        _publish_new(temporary, destination)
    finally:
        _cleanup_temporary(temporary)
    return {"backup": str(destination), "bytes": destination.stat().st_size}


def restore_database(database: str | Path, backup: str | Path) -> dict[str, Any]:
    database, backup = _path(database), _path(backup)
    _safe_parent(backup)
    _safe_file(backup)
    _check_sidecars(backup, reject=True)
    _safe_parent(database, create=True)
    secure_directory(database.parent)
    if database == backup or (database.exists() and os.path.samefile(database, backup)):
        raise ValueError("The backup and active database must be different files.")
    temporary = _temp_file(database)
    rollback: Path | None = None
    try:
        with database_lock(database, exclusive=True, timeout_seconds=10.0):
            _snapshot(backup, temporary)
            database_exists = _safe_file(database, required=False)
            _check_sidecars(database, reject=not database_exists)
            if database_exists:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                rollback = database.with_name(f"{database.name}.before-restore-{timestamp}-{uuid4().hex[:8]}.bak")
                rollback_temp = _temp_file(rollback)
                try:
                    _snapshot(database, rollback_temp, validate=False)
                    _publish_new(rollback_temp, rollback)
                finally:
                    _cleanup_temporary(rollback_temp)
                # The exclusive application lock means every cooperating engine
                # connection is closed. Checkpoint also detects active WAL readers.
                with closing(_connect(database)) as active:
                    checkpoint = active.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if checkpoint and checkpoint[0] != 0:
                        raise ValueError("Database has active SQLite connections; stop other clients and retry.")
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(database) + suffix)
                    if _safe_file(sidecar, required=False):
                        sidecar.unlink()
            _sync_file(temporary)
            os.replace(temporary, database)
            _sync_directory(database.parent)
            _safe_file(database)
            with closing(_connect(database)) as restored:
                _validate(restored, fts=True)
                # Keep the engine's persistent journal mode even for an idle
                # server process created before this restore.
                restored.execute("PRAGMA journal_mode=WAL")
    finally:
        _cleanup_temporary(temporary)
    return {
        "restored": str(database), "rollback_backup": str(rollback) if rollback else None,
        "notice": "Restart Codex memory server sessions after restore; stop other SQLite clients before restoring.",
    }


def export_memories(database: str | Path, destination: str | Path) -> dict[str, Any]:
    database, destination = _path(database), _path(destination)
    _safe_parent(database)
    _safe_file(database)
    _safe_parent(destination, create=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists; choose a new export filename.")
    with database_lock(database):
        _safe_parent(database)
        _safe_file(database)
        with closing(_connect(database, readonly=True)) as connection:
            connection.row_factory = sqlite3.Row
            memories = [dict(row) for row in connection.execute(
                "SELECT id, content, category, scope, project, source, created_at, updated_at, "
                "expires_at, superseded_by, status FROM memories ORDER BY created_at, id"
            )]
    temporary = _temp_file(destination)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"exported_at": datetime.now(timezone.utc).isoformat(), "memories": memories},
                      stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _publish_new(temporary, destination)
    finally:
        _cleanup_temporary(temporary)
    return {"export": str(destination), "memories": len(memories)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up, restore, inspect and export Codex Recall memory.")
    parser.add_argument("--db", help="Database path (default: CODEX_MEMORY_DB or XDG data directory).")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("backup", "restore", "export", "status"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--db", default=argparse.SUPPRESS)
        if command in ("backup", "export"):
            subparser.add_argument("destination")
        if command == "restore":
            subparser.add_argument("backup")
            subparser.add_argument("--confirm", action="store_true", help="Replace the active database, retaining a rollback backup.")
    arguments = parser.parse_args(argv)
    database = arguments.db or default_database_path()
    try:
        if arguments.command == "backup":
            result = backup_database(database, arguments.destination)
        elif arguments.command == "restore":
            if not arguments.confirm:
                raise ValueError("Restore replaces the active database. Re-run with --confirm after reviewing the backup.")
            result = restore_database(database, arguments.backup)
        elif arguments.command == "export":
            result = export_memories(database, arguments.destination)
        else:
            result = MemoryStore(database).memory_status()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if arguments.command == "status" and not (
            result.get("accessible")
            and result.get("integrity") == "ok"
            and result.get("fts_integrity") == "ok"
            and result.get("fts5_available")
        ):
            return 1
        return 0
    except sqlite3.DatabaseError:
        print("Memory maintenance failed: SQLite validation or backup failed. Check database integrity and permissions.", file=sys.stderr)
        return 1
    except (OSError, ValueError, TimeoutError, RuntimeError) as error:
        print(f"Memory maintenance failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
