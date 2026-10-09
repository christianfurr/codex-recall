"""Transactional SQLite storage with synchronized FTS and maintenance locks."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import SCHEMA_VERSION, migrate
from .models import (
    PUBLIC_FIELDS, duplicate_key, normalize_memory, utc_now, validate_filters,
    validate_id, validate_limit,
)
from .search import filter_sql, fts_queries
from .security import secure_directory, secure_file, secure_sqlite_files


SERVER_VERSION = "1.0.0"


def default_database_path() -> Path:
    override = os.environ.get("CODEX_MEMORY_DB")
    if override:
        return Path(override).expanduser().absolute()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home and Path(data_home).expanduser().is_absolute() else Path.home() / ".local" / "share"
    return base / "local-codex-memory" / "memory.db"


@contextmanager
def database_lock(path: Path | str, exclusive: bool = False,
                  timeout_seconds: float = 30.0) -> Iterator[None]:
    """Coordinate all connections with safe replacement by the restore CLI."""
    lock_path = Path(str(path) + ".maintenance.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("The maintenance lock must be owned by the current user.")
        os.fchmod(descriptor, 0o600)
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Memory database is busy. Retry later.") from None
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _public(row: sqlite3.Row | dict) -> dict:
    return {field: row[field] for field in PUBLIC_FIELDS}


class MemoryStore:
    """A small memory engine; connections are opened only for each operation."""

    def __init__(self, path: Path | str | None = None, max_results: int = 10) -> None:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 100:
            raise ValueError("max_results must be an integer between 1 and 100.")
        os.umask(0o077)
        self.path = Path(path).expanduser().absolute() if path is not None else default_database_path()
        self.max_results = max_results
        secure_directory(self.path.parent)
        with self._connection() as connection:
            # WAL remains persistent. Busy timeout handles simultaneous startup.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            migrate(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with database_lock(self.path):
            secure_sqlite_files(self.path)
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
                secure_sqlite_files(self.path)

    def remember(self, content: str, category: str, scope: str,
                 project: str | None = None, source: str | None = None,
                 expires_at: str | None = None) -> dict:
        record = normalize_memory(content, category, scope, project, source, expires_at)
        key = duplicate_key(record)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM memories WHERE duplicate_key=? AND status='active'", (key,)).fetchone()
            if existing:
                connection.commit()
                return {**_public(existing), "duplicate": True}
            now = utc_now()
            record.update(id=uuid.uuid4().hex, created_at=now, updated_at=now,
                          superseded_by=None, status="active", duplicate_key=key)
            columns = PUBLIC_FIELDS + ("duplicate_key",)
            connection.execute(
                "INSERT INTO memories(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                tuple(record[field] for field in columns),
            )
            connection.commit()
            return {**_public(record), "duplicate": False}

    def recall(self, query: str, scope: str | None = None, project: str | None = None,
               category: str | None = None, limit: int = 5,
               include_superseded: bool = False) -> list[dict]:
        limit = validate_limit(limit, self.max_results)
        scope, project, category = validate_filters(scope, project, category)
        if not isinstance(include_superseded, bool):
            raise ValueError("include_superseded must be a boolean.")
        prepared = fts_queries(query)
        if prepared is None:
            return []
        conditions, values = filter_sql(scope, project, category, include_superseded, utc_now())
        sql = """SELECT m.*,
            bm25(memories_fts,5.0,1.0,1.0)
            * CASE WHEN m.scope='project' AND m.project=? THEN 1.6
                   WHEN m.scope='global' AND m.category='preferences' THEN 1.25 ELSE 1.0 END
            * CASE WHEN m.category IN ('project_decisions','development_conventions') THEN 1.1 ELSE 1.0 END AS relevance
            FROM memories_fts JOIN memories m ON m.rowid=memories_fts.rowid
            WHERE memories_fts MATCH ? AND """ + conditions + " ORDER BY relevance ASC,m.id ASC LIMIT ?"
        with self._connection() as connection:
            for expression in dict.fromkeys(prepared):
                rows = connection.execute(sql, [project, expression, *values, limit]).fetchall()
                if rows:
                    return [_public(row) for row in rows]
        return []

    def list_memories(self, category: str | None = None, scope: str | None = None,
                      project: str | None = None, limit: int = 5, offset: int = 0,
                      include_superseded: bool = False) -> list[dict]:
        limit = validate_limit(limit, self.max_results)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer.")
        if not isinstance(include_superseded, bool):
            raise ValueError("include_superseded must be a boolean.")
        scope, project, category = validate_filters(scope, project, category)
        conditions, values = filter_sql(scope, project, category, include_superseded, utc_now())
        with self._connection() as connection:
            rows = connection.execute("SELECT m.* FROM memories m WHERE " + conditions +
                                      " ORDER BY m.updated_at DESC,m.id ASC LIMIT ? OFFSET ?", [*values, limit, offset]).fetchall()
        return [_public(row) for row in rows]

    def update_memory(self, id: str, content: str | None = None, **changes: object) -> dict:
        identifier = validate_id(id)
        allowed = {"category", "scope", "project", "source", "expires_at", "status", "superseded_by"}
        if set(changes) - allowed:
            raise ValueError("Unsupported memory update field.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("The requested memory does not exist.")
            candidate = dict(row)
            if content is not None:
                candidate["content"] = content
            candidate.update(changes)
            validated = normalize_memory(*(candidate[field] for field in ("content", "category", "scope", "project", "source", "expires_at")))
            candidate.update(validated)
            status = candidate["status"]
            if not isinstance(status, str) or status not in ("active", "superseded"):
                raise ValueError("status must be active or superseded.")
            replacement = candidate["superseded_by"]
            if status == "active" and replacement is not None:
                # A caller reactivating a historical memory must explicitly clear
                # the link to avoid silently discarding their submitted metadata.
                raise ValueError("Active memories cannot have a supersession link.")
            if status == "superseded" and replacement is None and (row["status"] != "superseded" or "superseded_by" in changes):
                raise ValueError("Superseding a memory requires a replacement ID.")
            if replacement is not None:
                validate_id(replacement)
                if replacement == identifier:
                    raise ValueError("A memory cannot supersede itself.")
                target = connection.execute("SELECT * FROM memories WHERE id=?", (replacement,)).fetchone()
                if target is None:
                    raise ValueError("The replacement memory does not exist.")
                if any(candidate[field] != target[field] for field in ("scope", "project", "category")):
                    raise ValueError("The replacement must have the same memory context.")
                # An existing chain may point to a subsequently superseded target.
                # New links must always point at an active, unexpired replacement.
                link_changed = replacement != row["superseded_by"] or row["status"] != "superseded"
                if link_changed and (target["status"] != "active" or (target["expires_at"] is not None and target["expires_at"] <= utc_now())):
                    raise ValueError("The replacement memory must be active and unexpired.")
                visited = {identifier}
                cursor = target
                while cursor is not None:
                    if cursor["id"] in visited:
                        raise ValueError("Supersession links cannot form a cycle.")
                    visited.add(cursor["id"])
                    cursor = connection.execute("SELECT * FROM memories WHERE id=?", (cursor["superseded_by"],)).fetchone() if cursor["superseded_by"] else None
            # Moving a referenced target to another context would invalidate its
            # predecessor's historical supersession relationship.
            predecessors = connection.execute("SELECT scope,project,category FROM memories WHERE superseded_by=?", (identifier,)).fetchall()
            if any(any(candidate[field] != predecessor[field] for field in ("scope", "project", "category")) for predecessor in predecessors):
                raise ValueError("A replacement memory cannot leave its predecessors' context.")
            candidate["duplicate_key"] = duplicate_key(candidate)
            if status == "active":
                duplicate = connection.execute("SELECT id FROM memories WHERE duplicate_key=? AND status='active' AND id!=?", (candidate["duplicate_key"], identifier)).fetchone()
                if duplicate:
                    raise ValueError("This update would duplicate an existing active memory.")
            candidate["updated_at"] = utc_now()
            columns = ("content", "category", "scope", "project", "source", "expires_at", "status", "superseded_by", "duplicate_key", "updated_at")
            connection.execute("UPDATE memories SET " + ",".join(field + "=?" for field in columns) + " WHERE id=?",
                               [*(candidate[field] for field in columns), identifier])
            connection.commit()
            return _public(candidate)

    def forget(self, id: str) -> dict:
        identifier = validate_id(id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute("DELETE FROM memories WHERE id=?", (identifier,)).rowcount > 0
            connection.commit()
        return {"id": identifier, "deleted": deleted}

    def memory_status(self) -> dict:
        result = {
            "accessible": False, "integrity": "unavailable", "schema_version": None,
            "active_count": 0, "superseded_count": 0, "expired_count": 0,
            "database_size_bytes": 0, "fts5_available": False,
            "fts_integrity": "unavailable", "server_version": SERVER_VERSION,
        }
        try:
            with self._connection() as connection:
                result["accessible"] = True
                result["schema_version"] = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                checks = [row[0] for row in connection.execute("PRAGMA integrity_check")]
                foreign_keys_ok = connection.execute("PRAGMA foreign_key_check").fetchone() is None
                result["integrity"] = "ok" if checks == ["ok"] and foreign_keys_ok else "failed"
                now = utc_now()
                counts = connection.execute("""SELECT
                    SUM(CASE WHEN status='active' AND (expires_at IS NULL OR expires_at>?) THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='superseded' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN expires_at IS NOT NULL AND expires_at<=? THEN 1 ELSE 0 END)
                    FROM memories""", (now, now)).fetchone()
                result.update(active_count=counts[0] or 0, superseded_count=counts[1] or 0, expired_count=counts[2] or 0)
                result["fts5_available"] = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone() is not None
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("INSERT INTO memories_fts(memories_fts,rank) VALUES('integrity-check',1)")
                    connection.commit()
                    result["fts_integrity"] = "ok"
                except sqlite3.Error:
                    connection.rollback()
                    result["fts_integrity"] = "failed"
                for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                    try:
                        result["database_size_bytes"] += candidate.stat().st_size
                    except FileNotFoundError:
                        # WAL/SHM can disappear as another connection closes.
                        continue
        except (sqlite3.Error, OSError, ValueError, TimeoutError):
            result["integrity"] = "failed"
        return result

    def backup(self, destination: Path | str) -> Path:
        target = Path(destination).expanduser().absolute()
        if target == self.path:
            raise ValueError("The backup destination must differ from the active database.")
        secure_directory(target.parent)
        # A destination is never silently overwritten, including a symlink.
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        try:
            with self._connection() as source:
                destination_connection = sqlite3.connect(target, timeout=30)
                try:
                    source.backup(destination_connection)
                    check = destination_connection.execute("PRAGMA integrity_check").fetchone()
                    if check is None or check[0] != "ok":
                        raise RuntimeError("The backup did not pass its database integrity check.")
                    destination_connection.commit()
                finally:
                    destination_connection.close()
            secure_file(target)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise
