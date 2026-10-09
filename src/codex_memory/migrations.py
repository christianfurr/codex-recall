"""Deterministic schema migrations serialized by SQLite's write transaction."""

from __future__ import annotations

import sqlite3

from .models import utc_now


SCHEMA_VERSION = 1


def migrate(connection: sqlite3.Connection) -> None:
    """Create schema version 1 atomically; reject unsupported newer versions.

    This first migration is additive. Future destructive migrations must take a
    SQLite backup before modifying the schema, using the maintenance lock.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError("The memory database requires a newer server version.")
        if version == 0:
            statements = (
                """CREATE TABLE memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL CHECK(category IN ('preferences','machine_setup','project_decisions','task_progress','lessons_learned','development_conventions','technical_context','general')),
                    scope TEXT NOT NULL CHECK(scope IN ('global','machine','project')),
                    project TEXT,
                    source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    superseded_by TEXT REFERENCES memories(id) ON DELETE SET NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
                    duplicate_key TEXT NOT NULL,
                    CHECK((scope='project' AND project IS NOT NULL) OR (scope!='project' AND project IS NULL)),
                    CHECK(status='superseded' OR superseded_by IS NULL),
                    CHECK(superseded_by IS NULL OR superseded_by!=id)
                )""",
                "CREATE UNIQUE INDEX active_duplicate ON memories(duplicate_key) WHERE status='active'",
                "CREATE INDEX memory_context ON memories(scope,project,category,status,updated_at DESC)",
                "CREATE INDEX memory_expiration ON memories(expires_at) WHERE expires_at IS NOT NULL",
                "CREATE INDEX memory_supersession ON memories(superseded_by) WHERE superseded_by IS NOT NULL",
                "CREATE VIRTUAL TABLE memories_fts USING fts5(content,category,project,content='memories',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2',prefix='2 3 4')",
                """CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid,content,category,project) VALUES(new.rowid,new.content,new.category,new.project);
                END""",
                """CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts,rowid,content,category,project) VALUES('delete',old.rowid,old.content,old.category,old.project);
                END""",
                """CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts,rowid,content,category,project) VALUES('delete',old.rowid,old.content,old.category,old.project);
                    INSERT INTO memories_fts(rowid,content,category,project) VALUES(new.rowid,new.content,new.category,new.project);
                END""",
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_version(version,applied_at) VALUES(?,?)", (SCHEMA_VERSION, utc_now()))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
