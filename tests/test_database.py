"""Transactional storage, scope, lifecycle, and concurrency regression tests."""

import concurrent.futures
import multiprocessing
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_memory.database import MemoryStore


def utc_time(delta_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def mixed_process_worker(arguments):
    """Each spawned process opens its own independent engine/SQLite sessions."""
    path, worker = arguments
    store = MemoryStore(path)
    ids = set()
    for iteration in range(30):
        record = store.remember(f"Multiprocessmarker shared fact {(iteration + worker) % 5}.", "general", "global")
        ids.add(record["id"])
        store.recall("Multiprocessmarker")
        store.list_memories()
        if iteration % 5 == 0:
            status = store.memory_status()
            if status["integrity"] != "ok" or status["fts_integrity"] != "ok":
                raise AssertionError("Concurrent process observed inconsistent storage")
    return ids


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "memory.db"
        self.store = MemoryStore(self.path)

    def save(self, content="Use Bun for new projects.", **metadata):
        options = {"category": "preferences", "scope": "global", "source": "unit test"}
        options.update(metadata)
        return self.store.remember(content, **options)

    def test_creation_has_stable_uuid_utc_timestamps_and_sqlite_metadata(self):
        memory = self.save()
        self.assertEqual(uuid.UUID(memory["id"]).version, 4)
        self.assertFalse(memory["duplicate"])
        for field in ("created_at", "updated_at"):
            parsed = datetime.fromisoformat(memory[field].replace("Z", "+00:00"))
            self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(memory["status"], "active")
        self.assertIsNone(memory["project"])
        self.assertIsNone(memory["superseded_by"])
        self.assertIsNone(memory["expires_at"])
        with sqlite3.connect(self.path) as database:
            database.row_factory = sqlite3.Row
            record = database.execute("SELECT * FROM memories WHERE id = ?", (memory["id"],)).fetchone()
        for field in ("id", "content", "category", "scope", "project", "source", "created_at", "updated_at"):
            self.assertEqual(record[field], memory[field])

    def test_normalized_exact_duplicate_does_not_add_a_row(self):
        first = self.save("Prefer   Bun\nfor new projects.")
        second = self.save("  Prefer Bun for new projects.  ")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(self.store.memory_status()["active_count"], 1)

    def test_duplicate_comparison_includes_metadata_and_project_context(self):
        first = self.save("Use stable tool versions.")
        different_source = self.save("Use stable tool versions.", source="another confirmed source")
        different_category = self.save("Use stable tool versions.", category="development_conventions")
        machine = self.save("Use stable tool versions.", scope="machine")
        project_a = self.save("Use stable tool versions.", scope="project", project="alpha")
        project_b = self.save("Use stable tool versions.", scope="project", project="beta")
        expiring = self.save("Use stable tool versions.", expires_at=utc_time(3600))
        self.assertEqual(len({item["id"] for item in (first, different_source, different_category, machine, project_a, project_b, expiring)}), 7)

    def test_project_identifiers_are_normalized_without_cross_project_merging(self):
        first = self.save(scope="project", project="  Project A  ")
        equivalent = self.save(scope="project", project="project-a")
        other = self.save(scope="project", project="project-b")
        self.assertEqual(first["project"], "project-a")
        self.assertEqual(first["id"], equivalent["id"])
        self.assertNotEqual(first["id"], other["id"])

    def test_reopening_and_reinitializing_preserves_existing_rows(self):
        original = self.save("Restart persistence verification.")
        reopened = MemoryStore(self.path)
        found = reopened.recall("Restart persistence")
        self.assertEqual([item["id"] for item in found], [original["id"]])
        self.assertEqual(reopened.memory_status()["schema_version"], 1)

    def test_update_preserves_identity_and_creation_time(self):
        old = self.save("Use legacybundler for development.")
        updated = self.store.update_memory(old["id"], content="Use modernbundler for development.", source="verified correction")
        self.assertEqual(updated["id"], old["id"])
        self.assertEqual(updated["created_at"], old["created_at"])
        self.assertNotEqual(updated["updated_at"], old["updated_at"])
        self.assertEqual(updated["source"], "verified correction")
        self.assertEqual(self.store.recall("legacybundler"), [])
        self.assertEqual(self.store.recall("modernbundler")[0]["id"], old["id"])

    def test_rejected_update_rolls_back_content_and_metadata(self):
        old = self.save("A confirmed safe preference.")
        with self.assertRaises(ValueError):
            self.store.update_memory(old["id"], content="Changed content must roll back.", scope="project", project=None)
        unchanged = self.store.list_memories()[0]
        for key in ("content", "scope", "project", "updated_at"):
            self.assertEqual(unchanged[key], old[key])
        self.assertEqual(self.store.recall("roll back"), [])

    def test_forget_removes_database_and_search_rows_and_is_idempotent(self):
        memory = self.save("Delete the unique forgetmarker.")
        self.assertTrue(self.store.forget(memory["id"])["deleted"])
        self.assertFalse(self.store.forget(memory["id"])["deleted"])
        self.assertFalse(self.store.forget(uuid.uuid4().hex)["deleted"])
        self.assertEqual(self.store.recall("forgetmarker"), [])
        self.assertEqual(self.store.list_memories(), [])
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM memories").fetchone()[0], 0)

    def test_invalid_writes_leave_no_memories(self):
        cases = [
            {"content": "   "},
            {"category": "invented_category"},
            {"scope": "unknown"},
            {"scope": "project", "project": None},
            {"scope": "global", "project": "wrong"},
            {"scope": "machine", "project": "wrong"},
            {"expires_at": "not-a-date"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save(**changes)
        self.assertEqual(self.store.memory_status()["active_count"], 0)

    def test_expired_memories_are_excluded_and_reported(self):
        expired = self.save("Expirationmarker historical preference.", expires_at=utc_time(-60))
        current = self.save("Expirationmarker current preference.", expires_at=utc_time(3600))
        self.assertEqual([item["id"] for item in self.store.recall("Expirationmarker")], [current["id"]])
        self.assertEqual([item["id"] for item in self.store.list_memories()], [current["id"]])
        self.assertNotEqual(expired["id"], current["id"])
        status = self.store.memory_status()
        self.assertEqual(status["expired_count"], 1)
        self.assertEqual(status["active_count"], 1)

    def test_supersession_retains_history_and_excludes_it_from_normal_recall(self):
        old = self.save("Orbit uses PeerJS transport.", category="project_decisions", scope="project", project="orbit")
        new = self.save("Orbit now uses Cloudflare SFU transport.", category="project_decisions", scope="project", project="orbit")
        changed = self.store.update_memory(old["id"], status="superseded", superseded_by=new["id"])
        self.assertEqual(changed["superseded_by"], new["id"])
        self.assertEqual(changed["status"], "superseded")
        self.assertEqual([item["id"] for item in self.store.recall("Orbit", project="orbit")], [new["id"]])
        history = self.store.recall("Orbit", project="orbit", include_superseded=True)
        self.assertEqual({item["id"] for item in history}, {old["id"], new["id"]})
        self.assertEqual(self.store.memory_status()["superseded_count"], 1)

    def test_supersession_rejects_self_cycles_missing_targets_and_wrong_scope(self):
        first = self.save("Decision one.")
        second = self.save("Decision two.")
        wrong_scope = self.save("Machine decision.", scope="machine")
        for target in (first["id"], uuid.uuid4().hex, wrong_scope["id"]):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.store.update_memory(first["id"], status="superseded", superseded_by=target)
        self.store.update_memory(first["id"], status="superseded", superseded_by=second["id"])
        with self.assertRaises(ValueError):
            self.store.update_memory(second["id"], status="superseded", superseded_by=first["id"])
        third = self.save("Decision three.")
        self.store.update_memory(second["id"], status="superseded", superseded_by=third["id"])
        self.assertEqual({item["id"] for item in self.store.list_memories()}, {third["id"], wrong_scope["id"]})

    def test_pagination_is_stable_and_sorted_by_latest_update(self):
        rows = [self.save(f"Pagination row number {index}.") for index in range(6)]
        newest = self.store.update_memory(rows[0]["id"], source="recent correction")
        page_one = self.store.list_memories(limit=3)
        page_two = self.store.list_memories(limit=3, offset=3)
        self.assertEqual(page_one[0]["id"], newest["id"])
        self.assertEqual(len(page_one), 3)
        self.assertEqual(len(page_two), 3)
        self.assertEqual(len({item["id"] for item in page_one + page_two}), 6)
        dates = [item["updated_at"] for item in page_one + page_two]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_concurrent_writers_preserve_distinct_records_and_deduplicate(self):
        def write_distinct(index):
            return MemoryStore(self.path).remember(f"Concurrent marker record {index}.", "general", "global")

        def write_duplicate(index):
            return MemoryStore(self.path).remember("Shared concurrent duplicate.", "general", "global")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            distinct = list(executor.map(write_distinct, range(18)))
            duplicate = list(executor.map(write_duplicate, range(12)))
        self.assertEqual(len({item["id"] for item in distinct}), 18)
        self.assertEqual(len({item["id"] for item in duplicate}), 1)
        self.assertEqual(sum(not item["duplicate"] for item in duplicate), 1)
        self.assertEqual(self.store.memory_status()["active_count"], 19)

    def test_independent_processes_mix_writes_search_and_health_checks(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as executor:
            workers = list(executor.map(mixed_process_worker, [(str(self.path), worker) for worker in range(4)]))
        self.assertEqual(len(set.union(*workers)), 5)
        self.assertEqual(self.store.memory_status()["active_count"], 5)
        self.assertEqual(self.store.memory_status()["fts_integrity"], "ok")

    def test_status_and_sqlite_integrity_are_healthy(self):
        self.save()
        status = self.store.memory_status()
        self.assertTrue(status["accessible"])
        self.assertEqual(status["integrity"], "ok")
        self.assertEqual(status["fts_integrity"], "ok")
        self.assertTrue(status["fts5_available"])
        self.assertEqual(status["schema_version"], 1)
        self.assertGreater(status["database_size_bytes"], 0)
        self.assertTrue(status["server_version"])
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(database.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertTrue(database.execute("PRAGMA foreign_key_list(memories)").fetchall())

    def test_update_cannot_create_an_exact_active_duplicate(self):
        first = self.save("First confirmed fact.")
        second = self.save("Second confirmed fact.")
        with self.assertRaises(ValueError):
            self.store.update_memory(second["id"], content=first["content"])
        self.assertEqual(self.store.memory_status()["active_count"], 2)
        self.assertEqual(self.store.recall("Second confirmed fact")[0]["id"], second["id"])

    def test_timezone_offsets_are_normalized_and_naive_dates_rejected(self):
        memory = self.save(expires_at="2099-02-03T09:30:00+06:00")
        expiration = datetime.fromisoformat(memory["expires_at"].replace("Z", "+00:00"))
        self.assertEqual(expiration, datetime(2099, 2, 3, 3, 30, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            self.save(expires_at="2099-02-03T09:30:00")

    def test_replacement_cannot_move_out_of_predecessor_context(self):
        old = self.save("Old context fact.", scope="project", project="alpha")
        replacement = self.save("New context fact.", scope="project", project="alpha")
        self.store.update_memory(old["id"], status="superseded", superseded_by=replacement["id"])
        with self.assertRaises(ValueError):
            self.store.update_memory(replacement["id"], project="beta")
        self.assertEqual(self.store.recall("New context fact", project="alpha")[0]["project"], "alpha")

    def test_deleting_replacement_keeps_historical_record_and_foreign_keys_valid(self):
        old = self.save("Old historical fact.")
        new = self.save("New replacement fact.")
        self.store.update_memory(old["id"], status="superseded", superseded_by=new["id"])
        self.store.forget(new["id"])
        history = self.store.list_memories(include_superseded=True)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], old["id"])
        self.assertEqual(history[0]["status"], "superseded")
        self.assertIsNone(history[0]["superseded_by"])
        self.assertEqual(self.store.list_memories(), [])
        self.assertEqual(self.store.memory_status()["fts_integrity"], "ok")


if __name__ == "__main__":
    unittest.main()
