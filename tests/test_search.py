"""Keyword search behavior, ranking, filtering, and safe query handling."""

import tempfile
import unittest
from pathlib import Path

from codex_memory.database import MemoryStore


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "data" / "memory.db"
        self.store = MemoryStore(self.path)

    def save(self, content, **metadata):
        options = {"category": "general", "scope": "global"}
        options.update(metadata)
        return self.store.remember(content, **options)

    def ids(self, query, **filters):
        return [item["id"] for item in self.store.recall(query, **filters)]

    def test_keywords_are_case_insensitive_and_equivalent_queries_are_consistent(self):
        first = self.save("Bun powers TypeScript applications.")
        self.save("Bun builds a smaller unrelated package.")
        expected = self.ids("bun typescript")
        self.assertEqual(expected, [first["id"]])
        self.assertEqual(self.ids("  BUN   TypeScript  "), expected)
        self.assertEqual(self.ids("typescript bun"), expected)

    def test_phrase_matches_adjacent_words(self):
        phrase = self.save("Prefer small focused commits for changes.")
        self.save("Prefer small changes and focused tasks with commits.")
        self.assertEqual(self.ids('"small focused commits"'), [phrase["id"]])

    def test_explicit_prefix_matches_complete_words(self):
        architecture = self.save("Architecture decisions are confirmed.")
        self.save("Unrelated system preference.")
        self.assertEqual(self.ids("architect*"), [architecture["id"]])
        self.assertEqual(self.ids("architect"), [])

    def test_multiword_fallback_returns_partial_matches_when_no_conjunction_exists(self):
        relevant = self.save("Toolchain uses Bun.")
        self.assertEqual(self.ids("toolchain imaginaryunmatchedterm"), [relevant["id"]])

    def test_empty_and_irrelevant_queries_return_no_results(self):
        self.save("A stored ordinary memory.")
        for query in ("", "   ", "unfindabletokenzz"):
            with self.subTest(query=query):
                self.assertEqual(self.store.recall(query), [])

    def test_malformed_fts_and_sql_queries_are_safe_and_preserve_records(self):
        memory = self.save("Alpha has a safe searchable configuration.")
        for query in ('"', '"alpha', "alpha OR", "NEAR(alpha,)", "alpha***", "(alpha", "content:alpha", "'; DROP TABLE memories; --", "*", "\x00"):
            with self.subTest(query=query):
                result = self.store.recall(query)
                self.assertIsInstance(result, list)
                self.assertLessEqual(len(result), 10)
        self.assertEqual(self.ids("alpha"), [memory["id"]])
        self.assertEqual(self.store.memory_status()["integrity"], "ok")

    def test_project_context_includes_global_and_excludes_other_projects(self):
        global_preference = self.save("Prefer Bun toolchain.", category="preferences")
        project_a = self.save("Alpha Bun toolchain decision.", scope="project", project="alpha", category="project_decisions")
        project_b = self.save("Beta Bun toolchain decision.", scope="project", project="beta", category="project_decisions")
        found = self.ids("Bun toolchain", project="alpha")
        self.assertIn(global_preference["id"], found)
        self.assertIn(project_a["id"], found)
        self.assertNotIn(project_b["id"], found)
        self.assertEqual(self.ids("Bun", scope="project", project="alpha"), [project_a["id"]])
        listed = self.store.list_memories(scope="project", project="alpha")
        self.assertEqual([item["id"] for item in listed], [project_a["id"]])

    def test_scope_and_category_filters_are_exact(self):
        global_memory = self.save("Scopefilter stable setup.", category="preferences")
        machine_memory = self.save("Scopefilter machine setup.", category="machine_setup", scope="machine")
        project_memory = self.save("Scopefilter project setup.", category="project_decisions", scope="project", project="alpha")
        self.assertEqual(self.ids("Scopefilter", scope="global"), [global_memory["id"]])
        self.assertEqual(self.ids("Scopefilter", scope="machine"), [machine_memory["id"]])
        self.assertEqual(self.ids("Scopefilter", category="project_decisions", project="alpha"), [project_memory["id"]])

    def test_content_category_and_project_are_indexed(self):
        content = self.save("Uniquecontentmarker is a durable lesson.")
        category = self.save("Follow local style.", category="development_conventions")
        project = self.save("Chosen transport is WebRTC.", scope="project", project="uniqueprojectmarker")
        self.assertEqual(self.ids("Uniquecontentmarker"), [content["id"]])
        self.assertIn(category["id"], self.ids("development"))
        self.assertEqual(self.ids("uniqueprojectmarker", project="uniqueprojectmarker"), [project["id"]])

    def test_project_relevance_wins_over_similarly_relevant_general_memory(self):
        self.save("Neon deployment workflow.")
        selected = self.save("Neon deployment workflow.", scope="project", project="neon")
        self.assertEqual(self.ids("Neon deployment workflow", project="neon")[0], selected["id"])

    def test_default_limit_maximum_limit_and_configured_limit(self):
        for number in range(16):
            self.save(f"Limitmarker record {number}.")
        self.assertEqual(len(self.store.recall("Limitmarker")), 5)
        self.assertEqual(len(self.store.recall("Limitmarker", limit=100)), 10)
        self.assertEqual(len(self.store.list_memories(limit=100)), 10)
        restricted = MemoryStore(self.path, max_results=3)
        self.assertEqual(len(restricted.recall("Limitmarker", limit=100)), 3)

    def test_invalid_filters_pagination_and_limits_are_rejected(self):
        for invalid in (0, -1, True, 1.5, "5"):
            with self.subTest(limit=invalid), self.assertRaises(ValueError):
                self.store.recall("safe", limit=invalid)
        for options in ({"scope": "wrong"}, {"category": "wrong"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.recall("safe", **options)
        for invalid in (-1, True, 0.5):
            with self.subTest(offset=invalid), self.assertRaises(ValueError):
                self.store.list_memories(offset=invalid)


if __name__ == "__main__":
    unittest.main()
