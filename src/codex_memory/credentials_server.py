"""Optional credential actions over MCP; passwords never enter the protocol."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import __version__
from .browser_logins import BrowserLogins
from .credentials import CredentialError, CredentialStore, normalize_login_url, validate_alias
from .sudo_runner import run_sudo

INSTRUCTIONS = (
    "Use saved credentials only for a login or privileged action requested by the owner. "
    "Never request, submit, reveal, or remember passwords in chat or tool arguments. "
    "The owner adds passwords locally with codex-recall-credentials. "
    "List saved aliases, sign into a saved HTTPS site by alias, or run an exact named sudo action "
    "previously allowed by the owner. No tool can create credentials or approve sudo commands. "
    "Sign-in submission does not confirm authentication; the owner may need to finish MFA or a passkey "
    "in the visible dedicated browser. Sudo results report exit status and discard command output. "
    "Saved credential availability does not authorize unrelated actions or changes to security controls."
)
ALIAS = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$", "maxLength": 64}


def _tool(name: str, description: str, properties: dict, required: list[str],
          *, read: bool = False, destructive: bool = False) -> types.Tool:
    return types.Tool(
        name=name, description=description,
        inputSchema={"type": "object", "properties": properties,
                     "required": required, "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=read, destructiveHint=destructive,
                                          openWorldHint=not read),
    )


TOOLS = [
    _tool("credential_status", "Check the local OS keyring without reading passwords or opening unlock prompts.", {}, [], read=True),
    _tool("list_logins", "List saved aliases, site origins, kinds and allowed sudo action names. No usernames or passwords.", {}, [], read=True),
    _tool("sign_in", "Submit a saved login in its dedicated visible browser on its exact HTTPS origin. The owner must request this login. MFA/passkeys may require attention; this does not guarantee authentication.", {"alias": ALIAS}, ["alias"]),
    _tool("run_sudo", "Run one exact named command previously allowed locally by the owner. Use only for the requested privileged task. Password and command output are withheld; returns exit status.", {"alias": ALIAS, "action": ALIAS}, ["alias", "action"], destructive=True),
    _tool("close_login_browser", "Close a dedicated login browser. Saved keyring credentials and browser session files are retained.", {"alias": ALIAS}, ["alias"], destructive=True),
]
VALIDATORS = {tool.name: Draft202012Validator(tool.inputSchema) for tool in TOOLS}


def public_profiles(profiles: object) -> list[dict]:
    """Validate and allowlist metadata, even when a backend returns extra fields."""
    if not isinstance(profiles, list):
        raise CredentialError("Credential metadata is unavailable.")
    safe = []
    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("kind") not in ("web", "sudo"):
            raise CredentialError("Credential metadata is unavailable.")
        alias = validate_alias(profile.get("alias"))
        actions = profile.get("actions", [])
        if not isinstance(actions, list):
            raise CredentialError("Credential metadata is unavailable.")
        action_names = sorted(validate_alias(action) for action in actions)
        origin = None
        if profile["kind"] == "web":
            _, origin = normalize_login_url(profile.get("origin"))
            if profile["origin"] != origin or action_names:
                raise CredentialError("Credential metadata is unavailable.")
        safe.append({"alias": alias, "kind": profile["kind"], "origin": origin,
                     "actions": action_names})
    if len({profile["alias"] for profile in safe}) != len(safe):
        raise CredentialError("Credential metadata is unavailable.")
    return sorted(safe, key=lambda profile: profile["alias"])


class CredentialRuntime:
    def __init__(self, store=None, browsers=None, sudo_action=None):
        self.store = store if store is not None else CredentialStore()
        self.browsers = browsers if browsers is not None else BrowserLogins()
        self.sudo_action = sudo_action if sudo_action is not None else run_sudo
        # Serialize uses and closes of live browser contexts and privileged actions.
        self.lock = asyncio.Lock()

    async def dispatch(self, name: str, arguments: dict) -> dict:
        if name == "credential_status":
            status = await asyncio.to_thread(self.store.status)
            return {"available": status.get("available") is True,
                    "locked": status.get("locked") is True,
                    "backend": "Linux Secret Service"}
        if name == "list_logins":
            return {"logins": public_profiles(await asyncio.to_thread(self.store.list_profiles))}
        alias = validate_alias(arguments.get("alias"))
        async with self.lock:
            if name == "close_login_browser":
                await self.browsers.close(alias)
                return {"alias": alias, "closed": True}
            profiles = public_profiles(await asyncio.to_thread(self.store.list_profiles))
            matching = next((profile for profile in profiles if profile["alias"] == alias), None)
            if matching is None:
                raise CredentialError("Saved login was not found.")
            if name == "sign_in":
                if matching["kind"] != "web":
                    raise CredentialError("A website login is required.")
                private = await asyncio.to_thread(self.store.load, alias)
                try:
                    if private.get("origin") != matching["origin"]:
                        raise CredentialError("The saved site changed. Refresh the saved login list and retry.")
                    result = await self.browsers.login(private)
                finally:
                    private.clear()
                status = result.get("status")
                if status not in ("submitted", "needs_attention"):
                    raise CredentialError("Browser login could not complete.")
                # Never forward arbitrary browser/backend diagnostics or page content.
                return {"alias": alias, "origin": matching["origin"], "status": status,
                        "browser_session": alias,
                        "authentication_confirmed": False}
            if name == "run_sudo":
                action = validate_alias(arguments.get("action"))
                if matching["kind"] != "sudo" or action not in matching["actions"]:
                    raise CredentialError("That sudo action has not been allowed locally.")
                private = await asyncio.to_thread(self.store.load, alias)
                cancel_event = threading.Event()
                worker = asyncio.create_task(asyncio.to_thread(self.sudo_action, private, action, cancel_event=cancel_event))
                try:
                    try:
                        result = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        cancel_event.set()
                        # Keep the secret payload alive until the worker has
                        # stopped its sudo process and one-use helper.
                        while not worker.done():
                            try:
                                await asyncio.shield(worker)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                # Preserve the original request cancellation
                                # after a worker reports a sanitized failure.
                                break
                        # Consume any sanitized worker error before re-raising
                        # the request's original cancellation.
                        if not worker.cancelled():
                            worker.exception()
                        raise
                finally:
                    private.clear()
                code = result.get("exit_code")
                status = result.get("status")
                if status not in ("succeeded", "failed", "timed_out", "canceled") or (code is not None and (isinstance(code, bool) or not isinstance(code, int))):
                    raise CredentialError("Privileged action could not complete.")
                return {"alias": alias, "action": action, "status": status, "exit_code": code}
        raise CredentialError("Unknown credential action.")

    async def close(self) -> None:
        await self.browsers.close()


def make_server(runtime: CredentialRuntime) -> Server:
    server = Server("Codex Recall Logins", version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        validator = VALIDATORS.get(name)
        if validator is None or not validator.is_valid(arguments):
            message = "Invalid credential action. Use an alias or a named allowed action; never supply password values."
        else:
            try:
                result = await runtime.dispatch(name, arguments)
                return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))], structuredContent=result, isError=False)
            except Exception:
                # Even third-party backend and browser exceptions may contain secrets.
                message = "Saved login action could not complete. Check the local keyring, saved alias and allowed action."
        return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)

    return server


async def run(runtime: CredentialRuntime) -> None:
    try:
        server = make_server(runtime)
        async with stdio_server() as (incoming, outgoing):
            await server.run(incoming, outgoing, server.create_initialization_options())
    finally:
        await runtime.close()


def main() -> None:
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(CredentialRuntime()))
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit("Saved login service could not start. Check the optional credential installation.") from None


if __name__ == "__main__":
    main()
