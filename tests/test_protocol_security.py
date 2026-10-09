"""Malformed protocol input must never echo raw credentials in diagnostics."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProtocolSecurityTests(unittest.TestCase):
    def test_malformed_call_never_echoes_credentials_to_protocol_or_stderr(self):
        secret = "password=synthetic-malformed-protocol-secret"
        with tempfile.TemporaryDirectory() as directory:
            servers = [("codex_memory.server", "remember", ["--db", str(Path(directory) / "data/memory.db")]),
                       ("codex_memory.credentials_server", "sign_in", [])]
            for module, tool, arguments in servers:
                with self.subTest(server=module):
                    messages = [
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": secret}},
                    ]
                    result = subprocess.run([sys.executable, "-m", module, *arguments], input="".join(json.dumps(value) + "\n" for value in messages), capture_output=True, text=True, timeout=10, env=os.environ.copy())
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn(secret, result.stdout + result.stderr)
                    self.assertEqual(result.stderr, "")
                    replies = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertTrue(any(item.get("id") == 2 and "error" in item for item in replies))
