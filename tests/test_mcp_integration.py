"""Real official-SDK MCP client tests against independent stdio processes.

Every test uses a temporary database and synthetic content. These checks test
the MCP transport and service, rather than a mocked in-process dispatcher.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from test_security import SYNTHETIC_SECRETS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = {"remember", "recall", "update_memory", "forget", "list_memories", "memory_status"}


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "memory.db"
        self.stderr_path = Path(self.temporary.name) / "server.stderr"

    @asynccontextmanager
    async def server_session(self):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codex_memory.server", "--db", str(self.path)],
            cwd=str(PROJECT_ROOT),
            env={"PYTHONPATH": str(PROJECT_ROOT / "src"), "CODEX_MEMORY_TEST_SECRET": "synthetic-environment-only-credential"},
        )
        with self.stderr_path.open("a", encoding="utf-8") as diagnostics:
            async with stdio_client(parameters, errlog=diagnostics) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                    initialization = await session.initialize()
                    self.assertTrue(initialization.capabilities.tools)
                    yield session

    async def call(self, session, name, arguments=None):
        result = await session.call_tool(name, arguments or {})
        self.assertFalse(result.isError, "MCP tool failed: " + str(result.content))
        if result.structuredContent is not None:
            return result.structuredContent
        texts = [part.text for part in result.content if part.type == "text"]
        self.assertEqual(len(texts), 1)
        return json.loads(texts[0])

    async def remember(self, session, content, **metadata):
        arguments = {"content": content, "category": "general", "scope": "global", "source": "official SDK integration test"}
        arguments.update(metadata)
        return await self.call(session, "remember", arguments)

    async def test_exactly_six_discoverable_tools_have_expected_schemas(self):
        async with self.server_session() as session:
            result = await session.list_tools()
            self.assertEqual({item.name for item in result.tools}, TOOL_NAMES)
            self.assertEqual(len(result.tools), 6)
            by_name = {item.name: item for item in result.tools}
            self.assertEqual(set(by_name["remember"].inputSchema["required"]), {"content", "category", "scope"})
            self.assertEqual(set(by_name["recall"].inputSchema["required"]), {"query"})
            for name in ("recall", "list_memories", "memory_status"):
                self.assertTrue(by_name[name].annotations.readOnlyHint)
            for item in result.tools:
                self.assertFalse(item.inputSchema["additionalProperties"])

    async def test_creation_recall_update_and_deletion_over_stdio(self):
        async with self.server_session() as session:
            original = await self.remember(session, "Beforemark Bun is the preferred build tool.", category="preferences")
            self.assertEqual(uuid.UUID(original["id"]).version, 4)
            self.assertEqual(original["category"], "preferences")
            self.assertEqual(original["scope"], "global")
            self.assertTrue(original["reference_only"])
            with sqlite3.connect(self.path) as connection:
                saved = connection.execute("SELECT content, source FROM memories WHERE id = ?", (original["id"],)).fetchone()
            self.assertEqual(saved, (original["content"], "official SDK integration test"))
            recalled = await self.call(session, "recall", {"query": "Beforemark Bun"})
            self.assertTrue(recalled["reference_only"])
            self.assertEqual(recalled["memories"][0]["id"], original["id"])
            updated = await self.call(session, "update_memory", {"id": original["id"], "content": "Aftermark Bun is the preferred build tool.", "source": None})
            self.assertEqual(updated["id"], original["id"])
            self.assertEqual(updated["created_at"], original["created_at"])
            self.assertNotEqual(updated["updated_at"], original["updated_at"])
            self.assertIsNone(updated["source"])
            self.assertEqual((await self.call(session, "recall", {"query": "Beforemark"}))["memories"], [])
            self.assertEqual((await self.call(session, "recall", {"query": "Aftermark"}))["memories"][0]["id"], original["id"])
            listed = await self.call(session, "list_memories", {})
            self.assertTrue(listed["reference_only"])
            self.assertEqual(listed["memories"][0]["id"], original["id"])
            self.assertTrue((await self.call(session, "forget", {"id": original["id"]}))["deleted"])
            self.assertFalse((await self.call(session, "forget", {"id": original["id"]}))["deleted"])
            self.assertEqual((await self.call(session, "recall", {"query": "Aftermark"}))["memories"], [])
            self.assertEqual((await self.call(session, "list_memories", {}))["memories"], [])

    async def test_records_persist_across_two_independent_server_processes(self):
        async with self.server_session() as first_session:
            original = await self.remember(first_session, "Persistentmarker survives a new process.")
        # The first stdio_client has fully exited and terminated its child.
        async with self.server_session() as second_session:
            recalled = await self.call(second_session, "recall", {"query": "Persistentmarker"})
            self.assertEqual(recalled["memories"][0]["id"], original["id"])
            self.assertEqual(recalled["memories"][0]["created_at"], original["created_at"])

    async def test_normalized_duplicates_return_same_id_without_growth(self):
        async with self.server_session() as session:
            first = await self.remember(session, "Duplicate marker\nwith Bun.")
            second = await self.remember(session, " Duplicate   marker with Bun. ")
            self.assertEqual(first["id"], second["id"])
            self.assertTrue(second["duplicate"])
            status = await self.call(session, "memory_status")
            self.assertEqual(status["active_count"], 1)

    async def test_project_global_machine_scopes_are_isolated(self):
        async with self.server_session() as session:
            global_memory = await self.remember(session, "Scopemarker prefers Bun.", category="preferences")
            machine = await self.remember(session, "Scopemarker machine has Bun.", category="machine_setup", scope="machine")
            alpha = await self.remember(session, "Scopemarker alpha uses Bun.", category="project_decisions", scope="project", project="alpha")
            beta = await self.remember(session, "Scopemarker beta uses npm.", category="project_decisions", scope="project", project="beta")
            result = await self.call(session, "recall", {"query": "Scopemarker", "project": "alpha"})
            ids = {item["id"] for item in result["memories"]}
            self.assertEqual(ids, {global_memory["id"], machine["id"], alpha["id"]})
            self.assertNotIn(beta["id"], ids)
            narrowed = await self.call(session, "recall", {"query": "Scopemarker", "scope": "project", "project": "alpha"})
            self.assertEqual([item["id"] for item in narrowed["memories"]], [alpha["id"]])
            listing = await self.call(session, "list_memories", {"scope": "project", "project": "beta"})
            self.assertEqual([item["id"] for item in listing["memories"]], [beta["id"]])

    async def test_expired_memories_are_excluded_and_expiration_can_be_cleared(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        async with self.server_session() as session:
            expired = await self.remember(session, "Expirymarker was temporary.", expires_at=timestamp)
            current = await self.remember(session, "Expirymarker is still current.")
            result = await self.call(session, "recall", {"query": "Expirymarker"})
            self.assertEqual([item["id"] for item in result["memories"]], [current["id"]])
            self.assertEqual((await self.call(session, "memory_status"))["expired_count"], 1)
            await self.call(session, "update_memory", {"id": expired["id"], "expires_at": None})
            result = await self.call(session, "recall", {"query": "Expirymarker"})
            self.assertEqual({item["id"] for item in result["memories"]}, {expired["id"], current["id"]})

    async def test_secret_errors_and_schema_errors_never_echo_submitted_secrets(self):
        async with self.server_session() as session:
            for secret in SYNTHETIC_SECRETS:
                result = await session.call_tool("remember", {"content": secret, "category": "general", "scope": "global"})
                self.assertTrue(result.isError)
                self.assertNotIn(secret, result.model_dump_json())
            schema_secret = "synthetic-schema-value-should-never-be-echoed"
            invalid_arguments = (
                {"content": schema_secret, "category": schema_secret, "scope": "global"},
                {"content": {"secret": schema_secret}, "category": "general", "scope": "global"},
                {"content": "safe", "category": "general", "scope": "global", "extra": schema_secret},
            )
            for arguments in invalid_arguments:
                result = await session.call_tool("remember", arguments)
                self.assertTrue(result.isError)
                self.assertNotIn(schema_secret, result.model_dump_json())
            self.assertEqual((await self.call(session, "memory_status"))["active_count"], 0)
        diagnostics = self.stderr_path.read_text(encoding="utf-8")
        for secret in (*SYNTHETIC_SECRETS, schema_secret):
            self.assertNotIn(secret, diagnostics)

    async def test_supersession_is_explicit_and_history_is_available(self):
        async with self.server_session() as session:
            old = await self.remember(session, "Decisionmarker uses PeerJS.", category="project_decisions", scope="project", project="orbit")
            new = await self.remember(session, "Decisionmarker now uses Cloudflare SFU.", category="project_decisions", scope="project", project="orbit")
            updated = await self.call(session, "update_memory", {"id": old["id"], "status": "superseded", "superseded_by": new["id"]})
            self.assertEqual(updated["superseded_by"], new["id"])
            current = await self.call(session, "recall", {"query": "Decisionmarker", "project": "orbit"})
            self.assertEqual([item["id"] for item in current["memories"]], [new["id"]])
            history = await self.call(session, "recall", {"query": "Decisionmarker", "project": "orbit", "include_superseded": True})
            self.assertEqual({item["id"] for item in history["memories"]}, {old["id"], new["id"]})
            self.assertEqual((await self.call(session, "memory_status"))["superseded_count"], 1)

    async def test_health_reports_real_sqlite_and_fts_integrity_without_environment(self):
        async with self.server_session() as session:
            status = await self.call(session, "memory_status")
            self.assertTrue(status["accessible"])
            self.assertEqual(status["integrity"], "ok")
            self.assertEqual(status["fts_integrity"], "ok")
            self.assertTrue(status["fts5_available"])
            self.assertGreater(status["database_size_bytes"], 0)
            self.assertNotIn("synthetic-environment-only-credential", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
