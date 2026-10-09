"""Backup, restore, private export, and recovery safety on temporary data."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from codex_memory.database import MemoryStore, database_lock
from codex_memory.maintenance import backup_database, export_memories, restore_database


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "data" / "memory.db"
        self.backup = self.directory / "backups" / "snapshot.db"
        self.store = MemoryStore(self.path)
        self.original = self.store.remember("Backupmarker original durable fact.", "general", "global")

    def test_live_wal_backup_includes_committed_data_and_is_standalone_private(self):
        reader = sqlite3.connect(self.path)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM memories").fetchone()
            later = self.store.remember("Backupmarker committed during WAL reader.", "general", "global")
            self.assertTrue(Path(str(self.path) + "-wal").exists())
            result = backup_database(self.path, self.backup)
        finally:
            reader.rollback()
            reader.close()
        self.assertEqual(result["backup"], str(self.backup))
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backup.parent.stat().st_mode), 0o700)
        self.assertFalse(Path(str(self.backup) + "-wal").exists())
        with sqlite3.connect(self.backup) as connection:
            ids = {row[0] for row in connection.execute("SELECT id FROM memories")}
            self.assertEqual(ids, {self.original["id"], later["id"]})
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_restore_recovers_snapshot_and_preserves_pre_restore_rollback(self):
        backup_database(self.path, self.backup)
        extra = self.store.remember("Rollbackmarker added after snapshot.", "general", "global")
        result = restore_database(self.path, self.backup)
        rollback = Path(result["rollback_backup"])
        self.assertTrue(rollback.exists())
        self.assertEqual(stat.S_IMODE(rollback.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        fresh = MemoryStore(self.path)
        self.assertEqual(fresh.recall("Rollbackmarker"), [])
        self.assertEqual(fresh.recall("Backupmarker")[0]["id"], self.original["id"])
        self.assertEqual(fresh.memory_status()["fts_integrity"], "ok")
        with sqlite3.connect(rollback) as connection:
            ids = {row[0] for row in connection.execute("SELECT id FROM memories")}
        self.assertEqual(ids, {self.original["id"], extra["id"]})

    def test_restore_to_new_database_works_without_rollback(self):
        backup_database(self.path, self.backup)
        new_path = self.directory / "fresh" / "restored.db"
        result = restore_database(new_path, self.backup)
        self.assertIsNone(result["rollback_backup"])
        self.assertEqual(MemoryStore(new_path).recall("Backupmarker")[0]["id"], self.original["id"])

    def test_backup_refuses_existing_destinations(self):
        backup_database(self.path, self.backup)
        original_bytes = self.backup.read_bytes()
        with self.assertRaises(ValueError):
            backup_database(self.path, self.backup)
        self.assertEqual(self.backup.read_bytes(), original_bytes)

    def test_schema_and_fts_damage_prevent_publishing_backup(self):
        for damage in ("schema", "fts"):
            with self.subTest(damage=damage):
                damaged_path = self.directory / damage / "memory.db"
                damaged_store = MemoryStore(damaged_path)
                damaged_store.remember("A fact for integrity validation.", "general", "global")
                with sqlite3.connect(damaged_path) as connection:
                    if damage == "schema":
                        connection.execute("CREATE TABLE unexpected_schema (value TEXT)")
                    else:
                        connection.execute("DELETE FROM memories_fts WHERE rowid=1")
                destination = self.directory / "backups" / f"{damage}.db"
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    backup_database(damaged_path, destination)
                self.assertFalse(destination.exists())

    def test_invalid_restore_leaves_active_database_unchanged(self):
        invalid = self.directory / "invalid.db"
        invalid.write_bytes(b"This is a synthetic invalid SQLite backup")
        invalid.chmod(0o600)
        before = self.path.read_bytes()
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            restore_database(self.path, invalid)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.recall("Backupmarker")[0]["id"], self.original["id"])

    def test_restore_waits_for_active_application_lock_before_replacing(self):
        backup_database(self.path, self.backup)
        started = threading.Event()
        finished = threading.Event()
        results = []
        errors = []

        def restore():
            started.set()
            try:
                results.append(restore_database(self.path, self.backup))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        with database_lock(self.path):
            worker = threading.Thread(target=restore)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.15), "Restore replaced data while a memory operation still held its lock")
        worker.join(timeout=12)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(self.store.memory_status()["integrity"], "ok")

    def test_lock_timeout_is_bounded(self):
        with database_lock(self.path):
            with self.assertRaises(TimeoutError):
                with database_lock(self.path, exclusive=True, timeout_seconds=0.05):
                    self.fail("Conflicting maintenance lock was acquired")

    def test_restore_rejects_links_and_world_readable_backups(self):
        backup_database(self.path, self.backup)
        linked = self.backup.with_name("linked.db")
        linked.symlink_to(self.backup)
        with self.assertRaises(ValueError):
            restore_database(self.path, linked)
        linked.unlink()
        os.link(self.backup, linked)
        with self.assertRaises(ValueError):
            restore_database(self.path, linked)
        linked.unlink()
        self.backup.chmod(0o644)
        with self.assertRaises(ValueError):
            restore_database(self.path, self.backup)
        self.backup.chmod(0o600)
        self.assertEqual(self.store.memory_status()["active_count"], 1)

    def test_export_is_private_json_and_omits_internal_duplicate_keys(self):
        extra = self.store.remember("An inspectable project decision.", "project_decisions", "project", project="alpha")
        destination = self.directory / "exports" / "memories.json"
        result = export_memories(self.path, destination)
        self.assertEqual(result["memories"], 2)
        data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual({item["id"] for item in data["memories"]}, {self.original["id"], extra["id"]})
        self.assertTrue(data["exported_at"])
        for item in data["memories"]:
            self.assertNotIn("duplicate_key", item)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with self.assertRaises(ValueError):
            export_memories(self.path, destination)

    def run_status_cli(self):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, "-m", "codex_memory.maintenance", "--db", str(self.path), "status"],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_status_cli_returns_zero_for_healthy_database(self):
        result = self.run_status_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        status = json.loads(result.stdout)
        self.assertTrue(status["accessible"])
        self.assertEqual(status["integrity"], "ok")
        self.assertEqual(status["fts_integrity"], "ok")
        self.assertTrue(status["fts5_available"])

    def test_status_cli_returns_nonzero_for_damaged_fts_index(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM memories_fts WHERE rowid=1")
        result = self.run_status_cli()
        self.assertEqual(result.returncode, 1)
        status = json.loads(result.stdout)
        self.assertTrue(status["accessible"])
        self.assertEqual(status["integrity"], "ok")
        self.assertEqual(status["fts_integrity"], "failed")
        self.assertNotIn("Traceback", result.stderr)

    def test_status_cli_handles_corrupted_sqlite_without_traceback(self):
        self.path.write_bytes(b"Synthetic malformed SQLite test file")
        result = self.run_status_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Memory maintenance failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Synthetic malformed SQLite test file", result.stderr)


if __name__ == "__main__":
    unittest.main()
