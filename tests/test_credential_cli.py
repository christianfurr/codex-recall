"""Local credential entry is hidden, interactive, and never echoed on failure."""
import contextlib
import getpass
import io
import unittest
from unittest.mock import MagicMock, patch

from codex_memory import credential_cli
from codex_memory.credentials import CredentialError


class CredentialCLITests(unittest.TestCase):
    def invoke(self, arguments, *, terminal=True, secrets=(), error=None):
        output, diagnostics = io.StringIO(), io.StringIO()
        store = MagicMock()
        store.status.return_value = {"available": True, "locked": False, "password": "synthetic-status-secret"}
        store.list_profiles.return_value = [{"alias": "machine-sudo", "kind": "sudo", "origin": None, "actions": [], "password": "synthetic-extra-secret"}]
        store.remove.return_value = True
        store.revoke_sudo.return_value = True
        if error is not None:
            store.add_sudo.side_effect = error
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics), \
             patch.object(credential_cli, "CredentialStore", return_value=store), \
             patch.object(credential_cli.sys.stdin, "isatty", return_value=terminal), \
             patch.object(diagnostics, "isatty", return_value=terminal), \
             patch.object(credential_cli.getpass, "getpass", side_effect=secrets):
            try:
                credential_cli.main(arguments)
                code = 0
            except SystemExit as exit_status:
                code = exit_status.code
        return code, output.getvalue(), diagnostics.getvalue(), store

    def test_owner_enters_sudo_password_only_through_hidden_prompt(self):
        secret = "synthetic-cli-sudo-secret"
        code, out, err, store = self.invoke(["add", "machine-sudo", "--sudo"], secrets=[secret])
        self.assertEqual(code, 0)
        store.add_sudo.assert_called_once_with("machine-sudo", secret, replace=False)
        self.assertNotIn(secret, out + err)

    def test_web_username_and_password_are_hidden_and_never_printed(self):
        username, password = "synthetic-private-username", "synthetic-web-secret"
        code, out, err, store = self.invoke(["add", "site", "--url", "https://example.test/login"], secrets=[username, password])
        self.assertEqual(code, 0)
        store.add_web.assert_called_once_with("site", "https://example.test/login", username, password, replace=False)
        self.assertNotIn(username, out + err)
        self.assertNotIn(password, out + err)

    def test_noninteractive_entry_cannot_fall_back_to_echoed_stdin(self):
        code, out, err, store = self.invoke(["add", "machine-sudo", "--sudo"], terminal=False)
        self.assertEqual(code, 1)
        store.add_sudo.assert_not_called()

    def test_hidden_input_warning_fails_without_storing(self):
        code, out, err, store = self.invoke(["add", "machine-sudo", "--sudo"], secrets=[getpass.GetPassWarning("synthetic-warning-secret")])
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-warning-secret", out + err)
        store.add_sudo.assert_not_called()

    def test_provider_exception_even_credential_error_cannot_echo_secret(self):
        secret = "synthetic-provider-secret"
        for failure in (RuntimeError(secret), CredentialError(secret)):
            with self.subTest(kind=type(failure).__name__):
                code, out, err, store = self.invoke(["add", "machine-sudo", "--sudo"], secrets=[secret], error=failure)
                self.assertEqual(code, 1)
                self.assertNotIn(secret, out + err)

    def test_password_arguments_are_rejected_without_echo(self):
        secret = "synthetic-cli-argument-secret"
        code, out, err, store = self.invoke(["add", "machine-sudo", "--sudo", "--password", secret])
        self.assertEqual(code, 2)
        self.assertNotIn(secret, out + err)

    def test_invalid_origin_is_rejected_before_requesting_password(self):
        code, out, err, store = self.invoke(["add", "site", "--url", "http://example.test/login"])
        self.assertEqual(code, 1)
        store.add_web.assert_not_called()

    def test_status_and_listing_allowlist_safe_fields(self):
        for arguments in (["status"], ["list"]):
            code, out, err, store = self.invoke(arguments, terminal=False)
            self.assertEqual(code, 0)
            self.assertNotIn("synthetic-status-secret", out + err)
            self.assertNotIn("synthetic-extra-secret", out + err)

    def test_sudo_approval_rejects_shell_and_requires_owner_terminal(self):
        for terminal, command in ((False, "/usr/bin/id"), (True, "/bin/sh")):
            code, out, err, store = self.invoke(["allow", "machine-sudo", "check-host", "--", command], terminal=terminal)
            self.assertEqual(code, 1)
            store.allow_sudo.assert_not_called()

    def test_owner_can_allow_fixed_action_and_revoke_it(self):
        code, out, err, store = self.invoke(["allow", "machine-sudo", "check-host", "--", "/usr/bin/id"])
        self.assertEqual(code, 0)
        store.allow_sudo.assert_called_once_with("machine-sudo", "check-host", ["/usr/bin/id"], replace=False)
        code, out, err, store = self.invoke(["revoke", "machine-sudo", "check-host"])
        self.assertEqual(code, 0)
        store.revoke_sudo.assert_called_once_with("machine-sudo", "check-host")

    def test_action_replace_option_works_before_or_after_alias(self):
        for arguments in (["allow", "--replace", "machine-sudo", "check-host", "--", "/usr/bin/id"],
                          ["allow", "machine-sudo", "check-host", "--replace", "--", "/usr/bin/id"]):
            code, out, err, store = self.invoke(arguments)
            self.assertEqual(code, 0)
            store.allow_sudo.assert_called_once_with("machine-sudo", "check-host", ["/usr/bin/id"], replace=True)


if __name__ == "__main__":
    unittest.main()
