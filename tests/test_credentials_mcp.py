"""Real MCP transport proof: only aliases cross the credential protocol."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from codex_memory.credentials_server import CredentialRuntime


ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-credential-protocol-password"
USERNAME = "synthetic-private-login-username"
SCRIPT = '''
import asyncio, logging
from codex_memory.credentials_server import CredentialRuntime, run
logging.disable(logging.CRITICAL)
SECRET = "synthetic-credential-protocol-password"
USERNAME = "synthetic-private-login-username"
class Store:
    def status(self):
        return {"available": True, "locked": False, "password": SECRET}
    def list_profiles(self):
        return [{"alias":"site","kind":"web","origin":"https://example.test","actions":[],"password":SECRET,"username":USERNAME},
                {"alias":"broken","kind":"web","origin":"https://example.test","actions":[]},
                {"alias":"machine-sudo","kind":"sudo","origin":None,"actions":["check-host"]}]
    def load(self, alias):
        if alias == "broken": raise RuntimeError(SECRET)
        if alias == "site": return {"alias":alias,"kind":"web","username":USERNAME,"password":SECRET,"login_url":"https://example.test/login","origin":"https://example.test","actions":{}}
        return {"alias":alias,"kind":"sudo","password":SECRET,"actions":{"check-host":["/usr/bin/id"]}}
class Browser:
    async def login(self, profile):
        assert profile["password"] == SECRET
        return {"status":"submitted","password":SECRET,"username":USERNAME}
    async def close(self, alias=None): pass
def sudo_action(profile, action, **kwargs):
    assert profile["password"] == SECRET
    return {"status":"succeeded","exit_code":0,"stdout":SECRET,"stderr":USERNAME}
asyncio.run(run(CredentialRuntime(Store(), Browser(), sudo_action)))
'''


class CredentialMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.stderr = Path(self.temporary.name) / "stderr"

    @asynccontextmanager
    async def session(self):
        parameters = StdioServerParameters(command=sys.executable, args=["-c", SCRIPT],
            cwd=str(ROOT), env={"PYTHONPATH": str(ROOT / "src")})
        with self.stderr.open("a") as diagnostics:
            async with stdio_client(parameters, errlog=diagnostics) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
                    await session.initialize()
                    yield session

    def assert_private(self, result):
        text = result.model_dump_json()
        self.assertNotIn(SECRET, text)
        self.assertNotIn(USERNAME, text)
        self.assertNotIn(SECRET, self.stderr.read_text())

    async def test_five_tools_offer_actions_without_any_secret_input(self):
        async with self.session() as session:
            result = await session.list_tools()
            self.assertEqual({tool.name for tool in result.tools}, {"credential_status", "list_logins", "sign_in", "run_sudo", "close_login_browser"})
            for tool in result.tools:
                self.assertFalse(tool.inputSchema["additionalProperties"])
                self.assertTrue(set(tool.inputSchema["properties"]) <= {"alias", "action"})

    async def test_successful_actions_never_forward_private_adapter_fields(self):
        async with self.session() as session:
            calls = [("credential_status", {}), ("list_logins", {}),
                     ("sign_in", {"alias": "site"}),
                     ("run_sudo", {"alias": "machine-sudo", "action": "check-host"}),
                     ("close_login_browser", {"alias": "site"})]
            for name, args in calls:
                result = await session.call_tool(name, args)
                self.assertFalse(result.isError)
                self.assert_private(result)
                if name == "sign_in":
                    self.assertFalse(result.structuredContent["authentication_confirmed"])

    async def test_password_arguments_arbitrary_commands_and_unapproved_actions_fail(self):
        async with self.session() as session:
            calls = [("sign_in", {"alias": "site", "password": SECRET}),
                     ("run_sudo", {"alias": "machine-sudo", "action": "check-host", "command": SECRET}),
                     ("run_sudo", {"alias": "machine-sudo", "action": "unapproved"}),
                     ("remember_password", {"password": SECRET})]
            for name, args in calls:
                result = await session.call_tool(name, args)
                self.assertTrue(result.isError)
                self.assert_private(result)

    async def test_backend_error_containing_password_is_sanitized(self):
        async with self.session() as session:
            result = await session.call_tool("sign_in", {"alias": "broken"})
            self.assertTrue(result.isError)
            self.assert_private(result)


class CredentialRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_origin_is_rejected_before_browser_fills(self):
        payload = {"alias": "site", "kind": "web", "origin": "https://changed.test", "password": SECRET}
        class Store:
            def list_profiles(self):
                return [{"alias": "site", "kind": "web", "origin": "https://original.test", "actions": []}]
            def load(self, alias):
                return payload
        class Browser:
            async def login(self, profile):
                raise AssertionError("The changed site must not receive credentials")
        runtime = CredentialRuntime(store=Store(), browsers=Browser())
        with self.assertRaises(ValueError):
            await runtime.dispatch("sign_in", {"alias": "site"})
        self.assertEqual(payload, {})

    async def test_unknown_sudo_action_is_rejected_before_loading_password(self):
        class Store:
            calls = 0
            def list_profiles(self):
                return [{"alias": "machine-sudo", "kind": "sudo", "origin": None, "actions": ["check-host"]}]
            def load(self, alias):
                self.calls += 1
                raise AssertionError("Password must not be loaded")
        store = Store()
        runtime = CredentialRuntime(store=store)
        with self.assertRaises(ValueError):
            await runtime.dispatch("run_sudo", {"alias": "machine-sudo", "action": "other"})
        self.assertEqual(store.calls, 0)

    async def test_cancel_waits_for_privileged_worker_cleanup_before_discarding_payload(self):
        started, finished = threading.Event(), threading.Event()
        class Store:
            def list_profiles(self):
                return [{"alias": "machine-sudo", "kind": "sudo", "origin": None, "actions": ["check-host"]}]
            def load(self, alias):
                return {"alias": alias, "kind": "sudo", "password": SECRET, "actions": {"check-host": ["/usr/bin/id"]}}
        def worker(profile, action, *, cancel_event):
            started.set()
            if not cancel_event.wait(3):
                raise AssertionError("Cancellation not delivered")
            self.assertEqual(profile["password"], SECRET)
            finished.set()
            return {"status": "canceled", "exit_code": None}
        runtime = CredentialRuntime(store=Store(), sudo_action=worker)
        task = asyncio.create_task(runtime.dispatch("run_sudo", {"alias": "machine-sudo", "action": "check-host"}))
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_worker_failure_during_cleanup_preserves_cancellation(self):
        started = threading.Event()
        payload = {"alias": "machine-sudo", "kind": "sudo", "password": SECRET, "actions": {"check-host": ["/usr/bin/id"]}}
        class Store:
            def list_profiles(self):
                return [{"alias": "machine-sudo", "kind": "sudo", "origin": None, "actions": ["check-host"]}]
            def load(self, alias):
                return payload
        def worker(profile, action, *, cancel_event):
            started.set()
            cancel_event.wait(3)
            raise RuntimeError("Sanitized worker failure")
        runtime = CredentialRuntime(store=Store(), sudo_action=worker)
        task = asyncio.create_task(runtime.dispatch("run_sudo", {"alias": "machine-sudo", "action": "check-host"}))
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(payload, {})


if __name__ == "__main__":
    unittest.main()
