#!/usr/bin/env python3
"""Verify installed tools through two real Codex app-server processes.

No model turn, API call, or authentication read is requested. Other MCP servers
and plugins are disabled only for these test processes, preserving disk config.
The temporary expiring sentinel is removed at the end.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from uuid import uuid4

try:
    import tomllib
except ImportError:
    import tomli as tomllib

EXPECTED = {"remember", "recall", "update_memory", "forget", "list_memories", "memory_status"}


class Codex:
    def __init__(self, app: Path):
        config_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        config = tomllib.loads(config_path.read_text())
        if "local_memory" not in config.get("mcp_servers", {}):
            raise RuntimeError("Install Codex Recall before running this verification.")
        command = ["codex", "app-server", "--stdio", "-c", "analytics.enabled=false"]
        for name in config.get("mcp_servers", {}):
            if name != "local_memory":
                command += ["-c", f'mcp_servers.{name}.enabled=false']
        for name in config.get("plugins", {}):
            command += ["-c", f'plugins.{name}.enabled=false']
        # This installed Codex build exposes a process-scoped switch for its
        # remote-control transport; it does not alter the owner's settings.
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
            self.request("initialize", {"clientInfo": {"name": "codex_recall_verification", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
            started = self.request("thread/start", {"cwd": str(app), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only"})
            self.thread = started["thread"]["id"]
        except BaseException:
            self.close()
            raise

    def send(self, value: dict):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 45):
        self.identifier += 1
        identifier = self.identifier
        self.send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                raise RuntimeError("Codex app-server exited before verification finished.")
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"Codex app-server rejected {method}.")
                return message.get("result")
            if "id" in message and "method" in message:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "No interactive requests allowed during local verification."}})
        raise RuntimeError(f"Codex app-server timed out during {method}.")

    def inventory(self):
        result = self.request("mcpServerStatus/list", {"threadId": self.thread, "serverName": "local_memory", "detail": "toolsAndAuthOnly"})
        server = next(item for item in result["data"] if item["name"] == "local_memory")
        names = {value["name"] for value in server["tools"].values()}
        if names != EXPECTED:
            raise RuntimeError("Codex did not discover exactly the six expected tools.")
        return sorted(names)

    def call(self, name: str, arguments: dict):
        result = self.request("mcpServer/tool/call", {"threadId": self.thread, "server": "local_memory", "tool": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"Codex memory tool {name} reported an error.")
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


def main():
    from datetime import datetime, timedelta, timezone
    from codex_memory.database import MemoryStore

    app = Path(__file__).resolve().parents[1]
    marker = "recallverification" + uuid4().hex
    identifier = None
    first = second = None
    try:
        first = Codex(app)
        tools = first.inventory()
        stored = first.call("remember", {"content": f"Temporary Codex Recall verification marker {marker}", "category": "general", "scope": "global", "source": "local integration verification", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()})
        identifier = stored["id"]
        first.close()
        first = None
        second = Codex(app)
        assert second.inventory() == tools
        recalled = second.call("recall", {"query": marker})
        assert any(item["id"] == identifier for item in recalled["memories"])
        changed = second.call("update_memory", {"id": identifier, "content": f"Updated verification marker {marker}"})
        assert changed["id"] == identifier and changed["updated_at"] != stored["updated_at"]
        assert any(item["id"] == identifier for item in second.call("list_memories", {"category": "general"})["memories"])
        health = second.call("memory_status", {})
        assert health["integrity"] == health["fts_integrity"] == "ok"
        assert second.call("forget", {"id": identifier})["deleted"]
        assert not second.call("recall", {"query": marker})["memories"]
        print(json.dumps({"codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(), "six_tools_discovered": tools, "all_six_tools_called": True, "separate_codex_process_persistence": True, "sentinel_removed": True, "model_inference_used": False}, indent=2))
    finally:
        if first:
            first.close()
        if second:
            second.close()
        if identifier:
            MemoryStore().forget(identifier)


if __name__ == "__main__":
    main()
