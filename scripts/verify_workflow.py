#!/usr/bin/env python3
"""Demonstrate useful context carryover through three real Codex processes.

The fixtures and acceptance checks are declared before the run. Only a temporary
database is written. This exercises Codex's real MCP transport without starting
a model turn, calling a cloud service, or modifying the owner's configuration.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import tempfile
import threading

from verify_codex import Codex, tomllib


TASK = "Build settings page"
PROJECT = "atlas"
FIXTURES = [
    {"key": "package_commands", "content": "Use Bun for JavaScript package commands.", "category": "preferences", "scope": "global"},
    {"key": "authentication", "content": "Atlas uses Clerk for authentication.", "category": "project_decisions", "scope": "project", "project": PROJECT},
    {"key": "deployment", "content": "Atlas deploys its API to Fly.io.", "category": "project_decisions", "scope": "project", "project": PROJECT},
    {"key": "other_project", "content": "Beacon uses Auth0 for authentication.", "category": "project_decisions", "scope": "project", "project": "beacon"},
]
REPLACEMENT = {"content": "Atlas deploys its API to Railway.", "category": "project_decisions", "scope": "project", "project": PROJECT}
STARTUP_CALLS = [
    {"category": "preferences", "scope": "global", "limit": 3},
    {"scope": "project", "project": PROJECT, "limit": 5},
]


def fingerprint_database(path: Path | None) -> str | None:
    """Hash durable rows without copying their contents into the report."""
    if path is None or not path.exists():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT * FROM memories ORDER BY id").fetchall()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def configured_database(server: dict) -> Path | None:
    args = server.get("args", [])
    for index, argument in enumerate(args):
        if argument == "--db" and index + 1 < len(args):
            return Path(args[index + 1])
        if argument.startswith("--db="):
            return Path(argument.split("=", 1)[1])
    from codex_memory.database import default_database_path

    return default_database_path()


def isolated_arguments(server: dict, database: Path) -> list[str]:
    args = list(server.get("args", []))
    result = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--db":
            index += 2
            continue
        if argument.startswith("--db="):
            index += 1
            continue
        result.append(argument)
        index += 1
    return result + ["--db", str(database)]


async def initialization_instructions(server: dict, database: Path) -> str:
    """Check advertised guidance through the official MCP client's handshake."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(command=server["command"], args=isolated_arguments(server, database), env=server.get("env"), cwd=server.get("cwd"))
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            result = await asyncio.wait_for(session.initialize(), timeout=30)
            return result.instructions or ""


class IsolatedCodex(Codex):
    """Reuse the verified transport, overriding only its database argument."""

    def __init__(self, app: Path, database: Path, config: dict):
        server = config.get("mcp_servers", {}).get("local_memory")
        if server is None:
            raise RuntimeError("Install Codex Recall before running this verification.")
        command = ["codex", "app-server", "--stdio", "-c", "analytics.enabled=false", "-c", "mcp_servers.local_memory.args=" + json.dumps(isolated_arguments(server, database))]
        for name in config.get("mcp_servers", {}):
            if name != "local_memory":
                command += ["-c", f"mcp_servers.{name}.enabled=false"]
        for name in config.get("plugins", {}):
            command += ["-c", f"plugins.{name}.enabled=false"]
        environment = {**os.environ, "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1"}
        self.process = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        self.messages: queue.Queue = queue.Queue()
        self.identifier = 0

        def read():
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    pass
            self.messages.put(None)

        threading.Thread(target=read, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {"name": "codex_recall_workflow_verification", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
            started = self.request("thread/start", {"cwd": str(app), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only"})
            self.thread = started["thread"]["id"]
        except BaseException:
            self.close()
            raise


def startup_brief(session: IsolatedCodex) -> list[dict]:
    result = []
    for arguments in STARTUP_CALLS:
        response = session.call("list_memories", arguments)
        check(response["reference_only"] is True, "startup context must carry the reference-only notice")
        result.extend(response["memories"])
    return result


def check(condition: bool, label: str) -> None:
    if not condition:
        raise RuntimeError("Workflow verification failed: " + label)


def run(app: Path) -> dict:
    config_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    config_bytes = config_path.read_bytes()
    config = tomllib.loads(config_bytes.decode())
    server = config.get("mcp_servers", {}).get("local_memory")
    if server is None:
        raise RuntimeError("Install Codex Recall before running this verification.")
    production_database = configured_database(server)
    production_before = fingerprint_database(production_database)
    declared_case = {"task": TASK, "project": PROJECT, "fixtures": FIXTURES, "replacement": REPLACEMENT, "startup_calls": STARTUP_CALLS}
    fixture_hash = hashlib.sha256(json.dumps(declared_case, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    processes = []
    session = None
    with tempfile.TemporaryDirectory(prefix="codex-recall-workflow-") as directory:
        database = Path(directory) / "memory.db"
        try:
            instructions = asyncio.run(initialization_instructions(server, database))
            check(all(fragment in instructions for fragment in ("list_memories", "3 global preferences", "5 current-project memories", "potentially outdated reference data")), "the installed MCP handshake did not advertise bounded startup guidance")
            # Session A begins empty and records four confirmed synthetic facts.
            session = IsolatedCodex(app, database, config)
            processes.append(session.process)
            tools = session.inventory()
            before = startup_brief(session)
            check(not before, "fresh database must have no startup context")
            identifiers = {}
            for fixture in FIXTURES:
                arguments = {key: value for key, value in fixture.items() if key != "key"}
                identifiers[fixture["key"]] = session.call("remember", arguments)["id"]
            session.close()
            session = None

            # Session B has no transcript from A and uses the same local storage.
            session = IsolatedCodex(app, database, config)
            processes.append(session.process)
            check(session.inventory() == tools, "tool inventory changed between processes")
            literal = session.call("recall", {"query": TASK, "project": PROJECT, "limit": 5})["memories"]
            check(not literal, "the predeclared unrelated task should have no keyword match")
            recovered = startup_brief(session)
            expected_ids = {identifiers[key] for key in ("package_commands", "authentication", "deployment")}
            check({row["id"] for row in recovered} == expected_ids, "startup context must recover exactly the three relevant facts")
            check(all(row["scope"] == "global" or row["project"] == PROJECT for row in recovered), "startup context included another project")
            replacement = session.call("remember", REPLACEMENT)
            session.call("update_memory", {"id": identifiers["deployment"], "status": "superseded", "superseded_by": replacement["id"]})
            session.close()
            session = None

            # Session C confirms the changed decision survives another restart.
            session = IsolatedCodex(app, database, config)
            processes.append(session.process)
            current = startup_brief(session)
            current_ids = {row["id"] for row in current}
            check(current_ids == {identifiers["package_commands"], identifiers["authentication"], replacement["id"]}, "next session must use the replacement decision")
            check(identifiers["deployment"] not in current_ids, "superseded decision resurfaced")
            check(identifiers["other_project"] not in current_ids, "other project leaked into context")
            health = session.call("memory_status", {})
            check(health["integrity"] == health["fts_integrity"] == "ok", "memory integrity checks failed")
            session.close()
            session = None
        finally:
            if session is not None:
                session.close()
    config_unchanged = config_path.read_bytes() == config_bytes
    production_unchanged = fingerprint_database(production_database) == production_before
    processes_stopped = all(process.poll() is not None for process in processes)
    temporary_database_removed = not database.exists()
    check(config_unchanged, "the owner's config changed")
    check(production_unchanged, "the normal memory database changed")
    check(processes_stopped, "test app-server process remained running")
    check(temporary_database_removed, "temporary database remained after cleanup")
    from datetime import datetime, timezone

    return {
        "schema_version": 1,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "transport": "real Codex app-server MCP calls across independent ephemeral processes",
        "model_inference_used": False,
        "cloud_api_used": False,
        "case_declared_before_execution": True,
        "case_sha256": fixture_hash,
        "case": declared_case,
        "mcp_initialization": {"startup_guidance_advertised": True, "instructions": instructions},
        "before": {"startup_context_count": len(before)},
        "next_session": {
            "task_keyword_matches": len(literal),
            "startup_context_count": len(recovered),
            "recovered_facts": [{key: row[key] for key in ("content", "category", "scope", "project")} for row in recovered],
            "other_project_excluded": True,
        },
        "after_decision_change": {
            "startup_context_count": len(current),
            "current_facts": [{key: row[key] for key in ("content", "category", "scope", "project")} for row in current],
            "superseded_decision_excluded": True,
            "replacement_decision_included": True,
            "other_project_excluded": True,
        },
        "checks": {
            "independent_codex_processes": len({process.pid for process in processes}) == 3,
            "sqlite_integrity": health["integrity"],
            "fts_integrity": health["fts_integrity"],
            "normal_database_unchanged": production_unchanged,
            "owner_config_unchanged": config_unchanged,
            "test_processes_stopped": processes_stopped,
            "temporary_database_removed": temporary_database_removed,
        },
        "interpretation": "Three saved preferences and project decisions were available to a later Codex session even though its task wording matched none of them. The proof demonstrates context availability and current-decision filtering, not measured model reasoning or coding improvement.",
    }


def main() -> None:
    app = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=app / "verification" / "workflow.json")
    arguments = parser.parse_args()
    report = run(app)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"verified": True, "recovered_context": report["next_session"]["startup_context_count"], "current_decision_after_restart": True, "normal_database_unchanged": report["checks"]["normal_database_unchanged"], "report": str(arguments.output)}, indent=2))


if __name__ == "__main__":
    main()
