"""Use a stored web login in a separate, private Chromium profile.

Only ordinary top-level HTTPS POST forms are automated. The result describes
submission, never authentication: the owner completes MFA or other unusual
flows in the visible browser. Passwords and page contents are never returned.

HTTP(S) and WebSocket safeguards are not a complete network firewall. A
registered site is trusted with its password; its JavaScript can read fields.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .credentials import CredentialError, normalize_login_url, validate_alias
from .security import secure_directory, secure_file


_INVALID = "The stored web login profile is invalid."
_STATIC_RESOURCES = {"script", "stylesheet", "image", "font"}


def _origin(url: str) -> str | None:
    """Read an origin without copying URL paths or query values into errors."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname
        if not host:
            return None
        host = "[" + host.lower() + "]" if ":" in host else host.encode("idna").decode("ascii").lower()
        port = parsed.port
        return "https://" + host + (":" + str(port) if port not in (None, 443) else "")
    except (ValueError, UnicodeError):
        return None


def _private_profile(value: object) -> dict[str, str]:
    try:
        if not isinstance(value, dict) or value.get("kind") != "web":
            raise ValueError
        alias = validate_alias(value.get("alias"))
        url, origin = normalize_login_url(value.get("login_url"))
        if value.get("origin") != origin or value.get("login_url") != url:
            raise ValueError
        username, password = value.get("username"), value.get("password")
        if any(not isinstance(item, str) or not item or "\x00" in item
               for item in (username, password)):
            raise ValueError
        if len(username) > 1024 or len(password) > 16384:
            raise ValueError
        return {"alias": alias, "origin": origin, "login_url": url,
                "username": username, "password": password}
    except (ValueError, TypeError):
        raise CredentialError(_INVALID) from None


def _is_debug_environment(name: str) -> bool:
    return (name.startswith("DEBUG") or name.startswith("PW_TEST_")
            or name in {"PWDEBUG", "PLAYWRIGHT_TRACE_DIR", "PLAYWRIGHT_DEBUG_LOG", "NODE_OPTIONS"})


def _browser_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not _is_debug_environment(name)}


@contextmanager
def _quiet_driver_environment():
    # The Playwright driver inherits Python's environment when it starts.
    # Its protocol debug logger can otherwise print fill() arguments.
    removed = {name: value for name, value in os.environ.items() if _is_debug_environment(name)}
    for name in removed:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        os.environ.update(removed)


def _secure_profile_tree(directory: Path) -> None:
    """The private root also protects files created while Chromium is running."""
    secure_directory(directory)
    for root, directories, files in os.walk(directory, followlinks=False):
        for name in directories:
            path = Path(root) / name
            try:
                if not path.is_symlink():
                    secure_directory(path)
            except FileNotFoundError:
                # Chromium can remove a temporary directory after enumeration.
                # Ownership, symlink and other permission errors still fail.
                continue
        for name in files:
            path = Path(root) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                # Chromium writes state using temporary files and renames.
                # A disappeared entry needs no permission repair.
                continue
            # Chromium uses internal singleton symlinks and socket files.
            # Never follow those or change their external targets.
            if stat.S_ISREG(info.st_mode):
                secure_file(path)


# This script only returns a form index and booleans. It never reads values.
_DISCOVER_FORM = r"""() => {
    const visible = e => !!(e && e.isConnected && e.getClientRects().length &&
        e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
    const passwords = Array.from(document.querySelectorAll('input[type="password"]')).filter(visible);
    if (passwords.length !== 1 || !passwords[0].form) return -1;
    return Array.from(document.forms).indexOf(passwords[0].form);
}"""


# Repeated immediately before both fills and submission, with the same handles.
# Form-action overrides and DOM replacements must not change the approved target.
_CHECK_FORM = r"""({form, username, password, submit, origin, action}) => {
    const visible = e => !!(e && e.isConnected && e.getClientRects().length &&
        e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
    const usable = e => visible(e) && !e.disabled && !e.readOnly;
    if (location.origin !== origin || !visible(form) ||
        !usable(username) || !usable(password) || !usable(submit)) return false;
    if ([username, password, submit].some(e => e.form !== form)) return false;
    if (form.method.toLowerCase() !== 'post' || (form.target && form.target !== '_self')) return false;
    let target;
    try { target = new URL(submit.getAttribute('formaction') || form.action, location.href); }
    catch { return false; }
    if (target.origin !== origin || target.protocol !== 'https:' ||
        target.username || target.password || target.hash || (action && target.href !== action)) return false;
    if ((submit.getAttribute('formmethod') || form.method).toLowerCase() !== 'post' ||
        (submit.getAttribute('formtarget') && submit.getAttribute('formtarget') !== '_self')) return false;
    const inputs = Array.from(form.querySelectorAll('input'));
    const passwords = inputs.filter(e => e.type === 'password');
    const names = inputs.filter(e => visible(e) && ['text', 'email'].includes(e.type));
    const allPasswords = Array.from(document.querySelectorAll('input[type="password"]')).filter(visible);
    if (passwords.length !== 1 || passwords[0] !== password || allPasswords.length !== 1 ||
        names.length !== 1 || names[0] !== username || !username.name || !password.name ||
        username.name === password.name || inputs.some(e => e.type === 'file')) return false;
    const autocomplete = (password.autocomplete || '').toLowerCase().split(/\s+/);
    if (autocomplete.includes('new-password') || autocomplete.includes('one-time-code')) return false;
    const buttons = Array.from(form.querySelectorAll('button, input[type="submit"]')).filter(
        e => visible(e) && !e.disabled && e.type === 'submit');
    if (buttons.length !== 1 || buttons[0] !== submit) return false;
    const label = (submit.innerText || submit.value || '').trim().toLowerCase();
    if (!/^(sign\s*in|log\s*in|log\s*on|login)$/.test(label) ||
        /(?:^|\/)(?:signup|sign-up|register|registration)(?:\/|$)/i.test(location.pathname)) return false;
    return target.href;
}"""


@dataclass
class _Session:
    context: Any
    directory: Path
    origin: str
    strict: bool = False
    navigating: bool = False
    rejected_navigation: bool = False
    blocked_request: bool = False


ContextFactory = Callable[[Path, dict[str, Any]], Awaitable[Any]]


class BrowserLogins:
    """Private per-alias browser sessions, with no generic browser tool API.

    ``context_factory`` is a trusted test seam. Production uses a sandboxed
    Chromium persistent context; TLS verification remains enabled. Closing a
    session retains its private cookies on disk for future owner use.
    """

    def __init__(self, data_dir: Path | None = None, *, headless: bool = False,
                 context_factory: ContextFactory | None = None):
        configured = os.environ.get("XDG_DATA_HOME")
        base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local/share"
        self.data_dir = Path(data_dir) if data_dir is not None else base / "codex-recall/browser-profiles"
        self._headless = headless
        self._context_factory = context_factory
        self._playwright: Any = None
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    def _result(self, profile: dict[str, str], status: str, reason: str | None = None) -> dict[str, str]:
        result = {"alias": profile["alias"], "origin": profile["origin"],
                  "browser_session": profile["alias"], "status": status}
        if reason:
            result["reason"] = reason
        return result

    async def _new_session(self, profile: dict[str, str]) -> _Session:
        secure_directory(self.data_dir)
        identifier = hashlib.sha256((profile["alias"] + "\x00" + profile["origin"]).encode()).hexdigest()
        directory = self.data_dir / identifier
        secure_directory(directory)
        options = {"headless": self._headless, "chromium_sandbox": True,
                   "ignore_https_errors": False, "service_workers": "block",
                   "accept_downloads": False, "env": _browser_environment(),
                   "args": ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]}
        if self._context_factory is not None:
            context = await self._context_factory(directory, options)
        else:
            if self._playwright is None:
                from playwright.async_api import async_playwright
                with _quiet_driver_environment():
                    self._playwright = await async_playwright().start()
            context = await self._playwright.chromium.launch_persistent_context(str(directory), **options)
        session = _Session(context, directory, profile["origin"])
        try:
            async def guard(route: Any) -> None:
                await self._guard_request(session, route)
            await context.route("**/*", guard)
            # Existing WebSocket connections can bypass HTTP routing later.
            # This feature requires the pinned optional Playwright version.
            await context.route_web_socket("**/*", self._block_websocket)
            _secure_profile_tree(directory)
        except Exception:
            with suppress(Exception):
                await context.close()
            raise
        self._sessions[profile["alias"]] = session
        return session

    async def _block_websocket(self, websocket: Any) -> None:
        await websocket.close()

    async def _guard_request(self, session: _Session, route: Any) -> None:
        request = route.request
        same_origin = _origin(request.url) == session.origin
        navigation = request.is_navigation_request()
        redirected = navigation and session.navigating and request.redirected_from is not None
        allowed = same_origin and not redirected
        if not same_origin and not session.strict and not navigation:
            static = request.method == "GET" and request.resource_type in _STATIC_RESOURCES and _origin(request.url) is not None
            if static:
                # Persisted shared-domain cookies or HTTP authorization must
                # not be sent to another origin during initial asset loading.
                credentials = await asyncio.gather(*(request.header_value(name)
                    for name in ("cookie", "authorization", "proxy-authorization")))
                allowed = not any(credentials) and not session.strict
        if not allowed:
            session.blocked_request = True
            if navigation:
                session.rejected_navigation = True
            await route.abort("blockedbyclient")
        else:
            await route.fallback()

    async def _form(self, page: Any) -> dict[str, Any] | None:
        index = await page.evaluate(_DISCOVER_FORM)
        if not isinstance(index, int) or index < 0:
            return None
        forms = await page.query_selector_all("form")
        if index >= len(forms):
            return None
        form = forms[index]
        passwords = await form.query_selector_all('input[type="password"]')
        names = await form.query_selector_all('input[type="text"], input:not([type]), input[type="email"]')
        names = [field for field in names if await field.is_visible()]
        buttons = await form.query_selector_all('button, input[type="submit"]')
        buttons = [button for button in buttons if await button.is_visible()
                   and await button.evaluate("e => e.type === 'submit' && !e.disabled")]
        if len(passwords) != 1 or len(names) != 1 or len(buttons) != 1:
            return None
        return {"form": form, "username": names[0], "password": passwords[0], "submit": buttons[0]}

    async def _ready(self, page: Any, fields: dict[str, Any], session: _Session,
                     action: str | None = None) -> str | None:
        if session.rejected_navigation or _origin(page.url) != session.origin:
            return None
        for frame in page.frames:
            if frame != page.main_frame and frame.url not in ("", "about:blank") and _origin(frame.url) != session.origin:
                return None
        value = await page.evaluate(_CHECK_FORM, {**fields, "origin": session.origin, "action": action})
        return value if isinstance(value, str) else None

    async def login(self, profile: dict) -> dict[str, str]:
        """Submit stored credentials only to their registered origin and form."""
        private = _private_profile(profile)
        async with self._lock:
            try:
                alias = private["alias"]
                session = self._sessions.get(alias)
                if session is not None and session.origin != private["origin"]:
                    await self._close_one(alias)
                    session = None
                if session is None:
                    session = await self._new_session(private)
                # Earlier sessions retain strict routing; it is never relaxed.
                session.rejected_navigation = False
                session.blocked_request = False
                page = await session.context.new_page()
                session.navigating = True
                try:
                    response = await page.goto(private["login_url"], wait_until="domcontentloaded", timeout=20000)
                finally:
                    session.navigating = False
                if (session.rejected_navigation or page.url != private["login_url"]
                        or (response is not None and response.request.redirected_from is not None)):
                    return self._result(private, "needs_attention", "redirect_or_origin_changed")
                fields = await self._form(page)
                if fields is None:
                    return self._result(private, "needs_attention", "manual_sign_in_required")
                action = await self._ready(page, fields, session)
                if action is None:
                    return self._result(private, "needs_attention", "manual_sign_in_required")
                # From this point the HTTP(S) guard rejects every cross-origin
                # request, including encoded payloads. WebSockets are blocked
                # separately; routing is not a firewall for every browser API.
                session.strict = True
                if await self._ready(page, fields, session, action) is None:
                    return self._result(private, "needs_attention", "form_changed")
                await fields["username"].fill(private["username"], timeout=5000)
                if await self._ready(page, fields, session, action) is None:
                    return self._result(private, "needs_attention", "form_changed")
                await fields["password"].fill(private["password"], timeout=5000)
                if await self._ready(page, fields, session, action) is None:
                    return self._result(private, "needs_attention", "form_changed")
                await fields["submit"].click(timeout=10000)
                if session.rejected_navigation:
                    return self._result(private, "needs_attention", "request_blocked")
                return self._result(private, "submitted")
            except Exception:
                # Playwright failures can include request URLs or fill values.
                # Never stringify, chain or log the original exception.
                return self._result(private, "needs_attention", "browser_or_page_unavailable")
            finally:
                # Avoid retaining a second copy after this call completes.
                private.pop("username", None)
                private.pop("password", None)

    async def _close_one(self, alias: str) -> None:
        session = self._sessions.pop(alias, None)
        if session is not None:
            with suppress(Exception):
                await session.context.close()
            with suppress(Exception):
                _secure_profile_tree(session.directory)

    async def close(self, alias: str | None = None) -> None:
        """Close owned sessions; private persisted cookies remain on disk."""
        if alias is not None:
            alias = validate_alias(alias)
        async with self._lock:
            for name in ([alias] if alias is not None else list(self._sessions)):
                await self._close_one(name)
            if not self._sessions and self._playwright is not None:
                with suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None
