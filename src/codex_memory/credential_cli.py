"""Owner-managed password entry and exact sudo approvals; no secret output."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import warnings

from .credentials import CredentialError, CredentialStore, normalize_login_url, validate_alias
from .credentials_server import public_profiles
from .sudo_runner import validate_action


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid credential command. Run codex-recall-credentials --help.\n")


def _owner_terminal() -> None:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CredentialError("Use your interactive terminal to manage saved credentials.")


def _secret(prompt: str) -> str:
    _owner_terminal()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(prompt, stream=sys.stderr)
        except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
            raise CredentialError("Hidden credential entry was canceled or unavailable.") from None
    if not isinstance(value, str) or not value:
        raise CredentialError("Credential entry cannot be empty.")
    return value


def build_parser() -> SafeParser:
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    commands.add_parser("status", help="Check keyring availability without unlocking it")
    commands.add_parser("list", help="List aliases and allowed sudo action names")
    add = commands.add_parser("add", help="Enter a password locally with hidden prompts")
    add.add_argument("alias", help="A safe name such as github or machine-sudo")
    mode = add.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url", help="Exact HTTPS login page for this website")
    mode.add_argument("--sudo", action="store_true", help="Save a machine sudo password")
    add.add_argument("--replace", action="store_true", help="Replace an existing credential of the same kind")
    allow = commands.add_parser("allow", help="Allow an exact fixed sudo command from your terminal",
                                epilog="Supply the fixed command after --: allow ALIAS ACTION [--replace] -- /absolute/executable arguments")
    allow.add_argument("alias")
    allow.add_argument("action")
    allow.add_argument("--replace", action="store_true", help="Replace an existing action with the same name")
    revoke = commands.add_parser("revoke", help="Revoke a saved sudo action")
    revoke.add_argument("alias")
    revoke.add_argument("action")
    remove = commands.add_parser("remove", help="Delete one profile and its saved password from the keyring")
    remove.add_argument("alias")
    return parser


def main(argv=None) -> None:
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    parser = build_parser()
    cli_args = list(sys.argv[1:] if argv is None else argv)
    command = None
    if cli_args and cli_args[0] == "allow" and "--" in cli_args:
        separator = cli_args.index("--")
        command = cli_args[separator + 1:]
        cli_args = cli_args[:separator]
    arguments = parser.parse_args(cli_args)
    try:
        store = CredentialStore()
        if arguments.command == "status":
            status = store.status()
            result = {"available": status.get("available") is True,
                      "locked": status.get("locked") is True,
                      "backend": "Linux Secret Service"}
        elif arguments.command == "list":
            result = {"logins": public_profiles(store.list_profiles())}
        else:
            _owner_terminal()
            alias = validate_alias(arguments.alias)
            if arguments.command == "add":
                if arguments.sudo:
                    password = _secret("Sudo password (hidden): ")
                    store.add_sudo(alias, password, replace=arguments.replace)
                    kind = "sudo"
                else:
                    normalize_login_url(arguments.url)
                    username = _secret("Login username (hidden): ")
                    password = _secret("Login password (hidden): ")
                    store.add_web(alias, arguments.url, username, password, replace=arguments.replace)
                    kind = "web"
                result = {"saved": True, "alias": alias, "kind": kind}
            elif arguments.command == "allow":
                action = validate_alias(arguments.action)
                command = validate_action(command)
                store.allow_sudo(alias, action, command, replace=arguments.replace)
                result = {"allowed": True, "alias": alias, "action": action}
            elif arguments.command == "revoke":
                action = validate_alias(arguments.action)
                result = {"revoked": store.revoke_sudo(alias, action), "alias": alias, "action": action}
            else:
                result = {"removed": store.remove(alias), "alias": alias}
        print(json.dumps(result, indent=2))
    except CredentialError:
        parser.exit(1, "Credential operation could not complete. Use an interactive terminal and check codex-recall-credentials status.\n")
    except KeyboardInterrupt:
        parser.exit(1, "Credential operation canceled.\n")
    except Exception:
        parser.exit(1, "Credential operation could not complete. Check the local keyring and setup.\n")


if __name__ == "__main__":
    main()
