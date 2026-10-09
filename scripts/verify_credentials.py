#!/usr/bin/env python3
"""Verify optional logins with synthetic keyring items and real Codex transport.

Requires an existing unlocked Linux Secret Service keyring and installation with
--credentials. Creates two temporary synthetic profiles in this app's namespace,
reads only those profiles, then deletes them. No real login or sudo is performed,
and no model turn, password output, or keyring unlock prompt is requested.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

from codex_memory.credentials import CredentialError, CredentialStore
from codex_memory.sudo_runner import validate_action
from verify_codex import Codex

EXPECTED = {"credential_status", "list_logins", "sign_in", "run_sudo", "close_login_browser"}


def call(client, name, arguments, *, error=False):
    result = client.request("mcpServer/tool/call", {
        "threadId": client.thread, "server": "local_credentials", "tool": name,
        "arguments": arguments,
    })
    if bool(result.get("isError")) != error:
        raise RuntimeError("Credential transport verification failed.")
    if error:
        return result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def main():
    app = Path(__file__).resolve().parents[1]
    store = CredentialStore()
    status = store.status()
    if status.get("available") is not True or status.get("locked") is not False:
        raise SystemExit("An existing unlocked desktop keyring is required for this verification.")
    before = len(store.list_profiles())
    suffix = uuid4().hex
    web_alias, sudo_alias = "verify-web-" + suffix, "verify-sudo-" + suffix
    password = "synthetic-verification-" + uuid4().hex
    username = "synthetic-user-" + uuid4().hex
    created = []
    client = None
    try:
        store.add_web(web_alias, "https://example.com/login", username, password)
        created.append(web_alias)
        store.add_sudo(sudo_alias, password)
        created.append(sudo_alias)
        metadata = [item for item in store.list_profiles() if item["alias"] in created]
        serialized = json.dumps(metadata)
        assert len(metadata) == 2 and password not in serialized and username not in serialized
        private = store.load(web_alias)
        assert private["password"] == password and private["username"] == username
        private.clear()
        command = validate_action(["/usr/bin/id"])
        store.allow_sudo(sudo_alias, "check-host", command)
        store.add_sudo(sudo_alias, password, replace=True)
        private = store.load(sudo_alias)
        assert private["password"] == password and private["actions"] == {"check-host": command}
        private.clear()
        try:
            store.add_sudo(sudo_alias, password)
        except CredentialError:
            pass
        else:
            raise RuntimeError("Duplicate protection failed.")
        assert store.revoke_sudo(sudo_alias, "check-host")

        client = Codex(app, allowed_servers=("local_memory", "local_credentials"))
        result = client.request("mcpServerStatus/list", {
            "threadId": client.thread, "serverName": "local_credentials", "detail": "toolsAndAuthOnly",
        })
        server = next(item for item in result["data"] if item["name"] == "local_credentials")
        names = {item["name"] for item in server["tools"].values()}
        assert names == EXPECTED
        for tool in server["tools"].values():
            assert set(tool["inputSchema"]["properties"]) <= {"alias", "action"}
        assert call(client, "credential_status", {})["available"]
        result = call(client, "list_logins", {})
        assert password not in json.dumps(result) and username not in json.dumps(result)
        assert all(any(item["alias"] == alias for item in result["logins"]) for alias in created)
        # Failures are exercised before secret loading or browser/sudo launch.
        call(client, "sign_in", {"alias": "unknown-" + suffix}, error=True)
        call(client, "run_sudo", {"alias": sudo_alias, "action": "not-approved"}, error=True)
        assert call(client, "close_login_browser", {"alias": web_alias})["closed"]
        assert client.inventory()  # Six memory tools remain installed.
        evidence = {
            "codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
            "native_keyring_available": True, "native_keyring_unlocked": True,
            "synthetic_profiles_created": 2, "synthetic_round_trip_verified": True,
            "metadata_excludes_synthetic_secrets": True, "replacement_preserves_actions": True,
            "duplicate_creation_rejected": True, "action_revocation_verified": True,
            "credential_tools_discovered": sorted(names), "all_five_tools_called": True,
            "unknown_login_rejected": True, "unapproved_sudo_rejected": True,
            "six_memory_tools_preserved": True, "model_inference_used": False,
            "real_login_or_sudo_performed": False,
        }
    finally:
        if client:
            client.close()
        for alias in reversed(created):
            store.remove(alias)
    assert len(store.list_profiles()) == before
    evidence["synthetic_profiles_removed"] = True
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
