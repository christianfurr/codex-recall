"""Credential storage checks use fake adapters and synthetic secrets only."""

import copy
import concurrent.futures
import fcntl
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_memory.credentials import (
    APPLICATION, CredentialError, CredentialStore, SecretServiceBackend,
    normalize_login_url, validate_alias, validate_command,
)


PASSWORD = "synthetic-private-password-keep-out-of-results"
USERNAME = "synthetic-private-user@example.test"


class FakeBackend:
    """Minimal adapter: status, attributes-only list, private read/write, delete."""

    def __init__(self):
        self.entries = {}
        self.available = True
        self.locked = False
        self.secret_reads = 0
        self.writes = 0
        self.failure = None

    def _check(self):
        if self.failure:
            raise self.failure
        if not self.available:
            raise RuntimeError(PASSWORD)
        if self.locked:
            raise CredentialError("Credential storage is locked. Unlock your keyring through the desktop before retrying.")

    def status(self):
        if self.failure:
            raise self.failure
        return {"available": self.available, "locked": self.locked}

    def list_entries(self):
        self._check()
        return [copy.deepcopy(attributes) for attributes, _ in self.entries.values()]

    def read(self, alias):
        self._check()
        self.secret_reads += 1
        return copy.deepcopy(self.entries.get(alias))

    def write(self, alias, attributes, secret, *, replace=False):
        self._check()
        if alias in self.entries and not replace:
            raise RuntimeError("Collision " + PASSWORD)
        self.writes += 1
        self.entries[alias] = (copy.deepcopy(attributes), bytes(secret))

    def delete(self, alias):
        self._check()
        return self.entries.pop(alias, None) is not None


class FileBackend:
    """Synthetic subprocess fixture; never connect to a desktop keyring."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def status(self):
        return {"available": True, "locked": False}

    def list_entries(self):
        return [record[0] for record in self._read().values()]

    def read(self, alias):
        record = self._read().get(alias)
        # Give concurrent read/modify/write operations a real opportunity to
        # overlap when locking is absent.
        time.sleep(0.01)
        return (record[0], record[1].encode()) if record is not None else None

    def write(self, alias, attributes, secret, *, replace=False):
        records = self._read()
        if alias in records and not replace:
            raise CredentialError("That login alias already exists. Use explicit replacement or choose another alias.")
        records[alias] = [attributes, secret.decode()]
        time.sleep(0.01)
        self.path.write_text(json.dumps(records))

    def delete(self, alias):
        records = self._read()
        deleted = records.pop(alias, None) is not None
        self.path.write_text(json.dumps(records))
        return deleted


def credential_process_worker(arguments):
    backend_path, lock_path, operation, action = arguments
    store = CredentialStore(FileBackend(backend_path), lock_path=lock_path)
    if operation == "approve":
        store.allow_sudo("admin", action, ["/usr/bin/id"])
        return "approved"
    if operation == "revoke":
        return store.revoke_sudo("admin", action)
    try:
        store.add_sudo("admin", PASSWORD)
        return "created"
    except CredentialError:
        return "collision"


class CredentialValidationTests(unittest.TestCase):
    def test_aliases_are_small_safe_labels_without_echoing_invalid_input(self):
        for alias in ("github", "work-portal", "admin_2", "a" * 64):
            self.assertEqual(validate_alias(alias), alias)
        for alias in (None, 1, "", "GitHub", "../admin", "1admin", "a" * 65,
                      "password=" + PASSWORD, "hi\nthere", "ｇithub"):
            with self.subTest(alias=repr(alias)[:18]), self.assertRaises(CredentialError) as caught:
                validate_alias(alias)
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_login_urls_bind_exact_canonical_https_origins(self):
        cases = {
            "https://Example.COM/login": ("https://example.com/login", "https://example.com"),
            "HTTPS://example.com:443": ("https://example.com/", "https://example.com"),
            "https://example.com:8443/sign-in": ("https://example.com:8443/sign-in", "https://example.com:8443"),
            "https://[2001:0db8::1]:8443/login": ("https://[2001:db8::1]:8443/login", "https://[2001:db8::1]:8443"),
            "https://127.0.0.1/login": ("https://127.0.0.1/login", "https://127.0.0.1"),
            "https://bücher.example/login": ("https://xn--bcher-kva.example/login", "https://xn--bcher-kva.example"),
        }
        for value, expected in cases.items():
            with self.subTest(url=value):
                self.assertEqual(normalize_login_url(value), expected)
                self.assertEqual(normalize_login_url(expected[0]), expected)

    def test_unsafe_urls_and_secret_bearing_paths_are_rejected_without_echo(self):
        for value in (None, "", "http://example.com", "javascript:alert(1)", "//example.com",
                      "https://user:pass@example.com", "https://user@example.com",
                      "https://example.com?", "https://example.com#", "https://example.com/?x=y",
                      "https://example.com/#foo", "https://example.com\\@other.example",
                      "https://example.com/login\n", "https://example.com/%0afoo",
                      "https://example.com/%5cfoo", "https://example.com:0", "https://example.com:65536",
                      "https://example.com:", "https://example.com.", "https://127.1",
                      "https://0x7f000001", "https://0x7f.0.0.1", "https://127.0.0.0x1",
                      "https://0177.0.0.1", "https://[::1]suffix", "https://[::1%25zone]",
                      "https://example..com", "https://-bad.example", "https://bad_.example",
                      "https://example.com/password%3D" + PASSWORD):
            with self.subTest(url=repr(value)[:30]), self.assertRaises(CredentialError) as caught:
                normalize_login_url(value)
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_commands_are_literal_bounded_argv_and_never_contain_credentials(self):
        command = ["/usr/bin/apt-get", "update", ""]
        normalized = validate_command(command)
        self.assertEqual(normalized, command)
        self.assertIsNot(normalized, command)
        for command in ("/usr/bin/id", [], ["id"], [None], ["/usr/bin/id", "line\nother"],
                        ["/usr/bin/id", "password=" + PASSWORD],
                        ["/usr/bin/tool", "--password", PASSWORD],
                        ["/usr/bin/id", "x" * 4097], ["/usr/bin/id"] * 65):
            with self.subTest(command=repr(command)[:28]), self.assertRaises(CredentialError) as caught:
                validate_command(command)
            self.assertNotIn(PASSWORD, str(caught.exception))


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.store = CredentialStore(self.backend)

    def add_web(self, alias="github", **kwargs):
        return self.store.add_web(alias, "https://example.test/login", USERNAME, PASSWORD, **kwargs)

    def mutate_payload(self, alias, mutate):
        attributes, raw = self.backend.entries[alias]
        payload = json.loads(raw)
        mutate(payload)
        self.backend.entries[alias] = (attributes, json.dumps(payload).encode())

    def assert_private_error(self, call):
        with self.assertRaises(CredentialError) as caught:
            call()
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertNotIn(USERNAME, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_web_add_returns_only_safe_metadata_and_stores_private_payload_in_backend(self):
        result = self.add_web()
        self.assertEqual(result, {"alias": "github", "kind": "web", "origin": "https://example.test", "actions": []})
        attributes, raw = self.backend.entries["github"]
        self.assertEqual(attributes["application"], APPLICATION)
        self.assertNotIn(PASSWORD, json.dumps(attributes) + json.dumps(result))
        self.assertNotIn(USERNAME, json.dumps(attributes) + json.dumps(result))
        private = self.store.load("github")
        self.assertEqual(private["password"], PASSWORD)
        self.assertEqual(private["username"], USERNAME)
        self.assertEqual(private["version"], 1)

    def test_list_and_status_do_not_read_secrets_and_strip_backend_extras(self):
        self.add_web("zulu")
        self.store.add_sudo("admin", PASSWORD)
        self.backend.secret_reads = 0
        metadata = self.store.list_profiles()
        status = self.store.status()
        self.assertEqual(self.backend.secret_reads, 0)
        self.assertEqual([profile["alias"] for profile in metadata], ["admin", "zulu"])
        self.assertEqual(status, {"available": True, "locked": False})
        self.assertNotIn(PASSWORD, json.dumps(metadata) + json.dumps(status))
        self.assertNotIn(USERNAME, json.dumps(metadata) + json.dumps(status))

    def test_web_collision_requires_explicit_replace_and_does_not_read_existing_secret(self):
        self.add_web()
        old = copy.deepcopy(self.backend.entries)
        self.assert_private_error(lambda: self.add_web())
        self.assertEqual(self.backend.entries, old)
        self.assertEqual(self.backend.secret_reads, 0)
        self.store.add_web("github", "https://new.example.test/login", USERNAME, "synthetic-replacement", replace=True)
        self.assertEqual(len(self.backend.entries), 1)
        self.assertEqual(self.store.load("github")["origin"], "https://new.example.test")

    def test_kind_collision_is_refused_even_with_replacement(self):
        self.add_web()
        self.assert_private_error(lambda: self.store.add_sudo("github", PASSWORD, replace=True))
        self.store.add_sudo("admin", PASSWORD)
        self.assert_private_error(lambda: self.add_web("admin", replace=True))
        self.assertEqual(self.store.load("github")["kind"], "web")
        self.assertEqual(self.store.load("admin")["kind"], "sudo")

    def test_sudo_actions_are_fixed_and_password_replacement_preserves_approvals(self):
        self.store.add_sudo("admin", PASSWORD)
        command = ["/usr/bin/apt-get", "update"]
        approved = self.store.allow_sudo("admin", "refresh-packages", command)
        self.assertEqual(approved["actions"], ["refresh-packages"])
        command.append("malicious-later-mutation")
        self.assertEqual(self.store.load("admin")["actions"]["refresh-packages"], ["/usr/bin/apt-get", "update"])
        self.assert_private_error(lambda: self.store.allow_sudo("admin", "refresh-packages", ["/usr/bin/id"]))
        self.store.allow_sudo("admin", "refresh-packages", ["/usr/bin/id"], replace=True)
        self.store.add_sudo("admin", "synthetic-new-sudo-password", replace=True)
        private = self.store.load("admin")
        self.assertEqual(private["actions"], {"refresh-packages": ["/usr/bin/id"]})
        self.assertEqual(private["password"], "synthetic-new-sudo-password")
        self.assertNotIn(PASSWORD, json.dumps(self.store.list_profiles()))

    def test_web_profiles_cannot_approve_sudo_actions(self):
        self.add_web()
        self.assert_private_error(lambda: self.store.allow_sudo("github", "admin", ["/usr/bin/id"]))

    def test_sudo_approval_can_be_revoked_without_deleting_password(self):
        self.store.add_sudo("admin", PASSWORD)
        self.store.allow_sudo("admin", "identify", ["/usr/bin/id"])
        self.store.allow_sudo("admin", "refresh", ["/usr/bin/apt-get", "update"])
        self.assertTrue(self.store.revoke_sudo("admin", "identify"))
        self.assertFalse(self.store.revoke_sudo("admin", "identify"))
        self.assertEqual(self.store.list_profiles()[0]["actions"], ["refresh"])
        private = self.store.load("admin")
        self.assertEqual(private["password"], PASSWORD)
        self.assertEqual(private["actions"], {"refresh": ["/usr/bin/apt-get", "update"]})
        self.add_web()
        self.assert_private_error(lambda: self.store.revoke_sudo("github", "identify"))

    def test_credential_validation_happens_before_backend_operations(self):
        for username, password in (("", PASSWORD), (USERNAME, ""), (USERNAME, "bad\x00value"),
                                   ("x" * 1025, PASSWORD), (USERNAME, "x" * 16385)):
            with self.subTest(field="credential"):
                self.assert_private_error(lambda: self.store.add_web("github", "https://example.test", username, password))
        for password in ("", "x" * 1025, "bad\nvalue", "bad\rvalue", "bad\x00value"):
            self.assert_private_error(lambda: self.store.add_sudo("admin", password))
        self.assertEqual(self.backend.writes, 0)
        self.assertEqual(self.backend.secret_reads, 0)

    def test_boolean_replacement_is_explicit(self):
        for replace in (1, "yes", None):
            with self.subTest(replace=repr(replace)):
                self.assert_private_error(lambda: self.add_web(replace=replace))
                self.assert_private_error(lambda: self.store.add_sudo("admin", PASSWORD, replace=replace))
        self.assertEqual(self.backend.writes, 0)

    def test_unknown_alias_and_removal_return_sanitized_results(self):
        self.assert_private_error(lambda: self.store.load("missing"))
        self.add_web()
        self.assertEqual(self.store.remove("github"), {"alias": "github", "deleted": True})
        self.assertEqual(self.store.remove("github"), {"alias": "github", "deleted": False})
        self.assertEqual(self.store.list_profiles(), [])

    def test_unavailable_and_locked_backends_fail_without_secrets_or_partial_writes(self):
        self.backend.available = False
        self.assertEqual(self.store.status(), {"available": False, "locked": None})
        self.assert_private_error(lambda: self.add_web())
        self.backend.available = True
        self.backend.locked = True
        self.assertEqual(self.store.status(), {"available": True, "locked": True})
        self.assert_private_error(lambda: self.store.list_profiles())
        self.assert_private_error(lambda: self.store.remove("github"))
        self.assertEqual(self.backend.writes, 0)

    def test_backend_errors_containing_passwords_are_never_echoed(self):
        for error in (RuntimeError(PASSWORD + USERNAME), CredentialError(PASSWORD + USERNAME)):
            self.backend.failure = error
            self.assert_private_error(lambda: self.store.list_profiles())
            self.assert_private_error(lambda: self.store.load("github"))
            self.assert_private_error(lambda: self.store.remove("github"))
            self.assertEqual(self.store.status(), {"available": False, "locked": None})

    def test_payload_and_public_attributes_must_agree(self):
        self.add_web()
        old = copy.deepcopy(self.backend.entries["github"])
        mutations = (
            lambda profile: profile.update(alias="other"),
            lambda profile: profile.update(origin="https://other.test"),
            lambda profile: profile.update(login_url="https://other.test/login"),
            lambda profile: profile.update(version=True),
            lambda profile: profile.update(version=2),
            lambda profile: profile.update(kind="sudo"),
            lambda profile: profile.update(extra=PASSWORD),
            lambda profile: profile.update(actions={"unexpected": ["/usr/bin/id"]}),
            lambda profile: profile.update(password=""),
        )
        for mutate in mutations:
            self.backend.entries["github"] = copy.deepcopy(old)
            self.mutate_payload("github", mutate)
            self.assert_private_error(lambda: self.store.load("github"))

    def test_malformed_or_secret_bearing_attributes_never_reach_public_metadata(self):
        self.add_web()
        original = copy.deepcopy(self.backend.entries["github"])
        for field, value in (("schema", "2"), ("application", "other-program"), ("alias", "password=" + PASSWORD),
                             ("origin", "https://example.test/?password=" + PASSWORD),
                             ("kind", "unknown"), ("actions", json.dumps(["password=" + PASSWORD])),
                             ("actions", json.dumps(["dup", "dup"])), ("actions", "not-json"),
                             ("actions", json.dumps([1])), ("actions", "x" * 4097),
                             ("actions", "[" * 2000 + "]" * 2000)):
            attributes, raw = copy.deepcopy(original)
            attributes[field] = value
            self.backend.entries["github"] = (attributes, raw)
            self.assert_private_error(lambda: self.store.list_profiles())
            self.assert_private_error(lambda: self.store.load("github"))
        attributes, raw = copy.deepcopy(original)
        attributes["password"] = PASSWORD
        self.backend.entries["github"] = (attributes, raw)
        self.assert_private_error(lambda: self.store.list_profiles())

    def test_duplicate_alias_metadata_is_not_selected_arbitrarily(self):
        self.add_web()
        attributes, raw = self.backend.entries["github"]
        self.backend.entries["other-key"] = (copy.deepcopy(attributes), bytes(raw))
        self.assert_private_error(lambda: self.store.list_profiles())

    def test_corrupt_secret_bytes_and_duplicate_json_keys_are_rejected(self):
        self.add_web()
        attributes, raw = self.backend.entries["github"]
        for raw in (b"", b"invalid json " + PASSWORD.encode(), b"\xff", b"null", b"[]",
                    b"x" * (1024 * 1024 + 1),
                    b'{"version":1,"version":2,"password":"' + PASSWORD.encode() + b'"}'):
            self.backend.entries["github"] = (attributes, raw)
            self.assert_private_error(lambda: self.store.load("github"))


class NativeBackendIsolationTests(unittest.TestCase):
    """Test native adapter calls against fake SecretStorage, never real D-Bus."""

    def setUp(self):
        self.calls = []
        self.items = []
        self.locked = False
        test = self

        class Collection:
            def is_locked(self):
                return test.locked

            def search_items(self, query):
                test.calls.append(("search", query))
                return [item for item in test.items if all(item.attributes.get(key) == value for key, value in query.items())]

            def create_item(self, label, attributes, secret, **options):
                test.calls.append(("create", label, copy.deepcopy(attributes), options))
                test.items.append(Item(attributes, secret))

            def unlock(self):
                raise AssertionError("Must not unlock collections")

        class Item:
            def __init__(self, attributes, secret):
                self.attributes = copy.deepcopy(attributes)
                self.secret = secret

            def get_attributes(self):
                return copy.deepcopy(self.attributes)

            def is_locked(self):
                return test.locked

            def get_secret(self):
                test.calls.append(("secret-read", self.attributes.get("alias")))
                return self.secret

            def set_secret(self, secret, **kwargs):
                test.calls.append(("secret-write", self.attributes.get("alias")))
                self.secret = secret

            def set_attributes(self, attributes):
                self.attributes = copy.deepcopy(attributes)

            def delete(self):
                test.items.remove(self)

        self.Item = Item
        self.collection = Collection()

        class Connection:
            def close(self):
                test.calls.append(("close",))

        def existing(connection, alias):
            self.calls.append(("collection", alias))
            return self.collection

        self.module = types.SimpleNamespace(dbus_init=Connection, get_collection_by_alias=existing)
        self.patch = patch.dict(sys.modules, {"secretstorage": self.module})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = CredentialStore(lock_path=Path(self.temporary.name) / "private" / ".store.lock")

    def test_initialization_is_lazy_and_metadata_discovery_never_gets_secret(self):
        self.assertEqual(self.calls, [])
        self.store.add_web("github", "https://example.test/login", USERNAME, PASSWORD)
        self.calls.clear()
        self.store.list_profiles()
        self.assertFalse(any(call[0] == "secret-read" for call in self.calls))
        self.assertTrue(all(call[1] == "default" for call in self.calls if call[0] == "collection"))
        self.assertFalse(any(call[0] in ("create", "secret-write") for call in self.calls))

    def test_only_own_namespace_is_enumerated_read_replaced_or_removed(self):
        unrelated = self.Item({"application": "other-app", "alias": "github"}, PASSWORD.encode())
        self.items.append(unrelated)
        self.store.add_web("github", "https://example.test/login", USERNAME, PASSWORD)
        self.assertEqual(len(self.store.list_profiles()), 1)
        self.assertEqual(self.store.load("github")["kind"], "web")
        self.store.remove("github")
        self.assertEqual(self.items, [unrelated])
        for call in self.calls:
            if call[0] == "search":
                self.assertEqual(call[1]["application"], APPLICATION)

    def test_replacement_changes_metadata_without_creating_duplicate_item(self):
        self.store.add_web("github", "https://example.test/login", USERNAME, PASSWORD)
        self.store.add_web("github", "https://new.example.test/sign-in", USERNAME, PASSWORD, replace=True)
        self.assertEqual(len(self.items), 1)
        self.assertEqual(self.store.load("github")["origin"], "https://new.example.test")

    def test_standard_gnome_schema_attribute_is_normalized_only_at_native_boundary(self):
        self.store.add_web("github", "https://example.test/login", USERNAME, PASSWORD)
        self.assertEqual(self.items[0].attributes["xdg:schema"], "org.freedesktop.Secret.Generic")
        self.assertNotIn("xdg:schema", self.store.list_profiles()[0])
        self.assertEqual(self.store.load("github")["password"], PASSWORD)
        self.assertEqual(self.store.backend.list_entries()[0]["application"], APPLICATION)
        self.assertNotIn("xdg:schema", self.store.backend.list_entries()[0])
        # A provider without this optional attribute remains supported.
        self.items[0].attributes.pop("xdg:schema")
        self.assertEqual(self.store.load("github")["password"], PASSWORD)
        self.store.add_web("github", "https://new.example.test/login", USERNAME, PASSWORD, replace=True)
        self.assertEqual(self.items[0].attributes["xdg:schema"], "org.freedesktop.Secret.Generic")

    def test_unknown_provider_schema_or_extra_attributes_are_not_silently_discarded(self):
        self.store.add_sudo("admin", PASSWORD)
        original = copy.deepcopy(self.items[0].attributes)
        for metadata in ({**original, "xdg:schema": PASSWORD},
                         {**original, "xdg:schema": "other-schema"},
                         {**original, "xdg:schema": True},
                         {**original, "extra-password": PASSWORD}):
            self.items[0].attributes = metadata
            with self.assertRaises(CredentialError) as caught:
                self.store.list_profiles()
            self.assertNotIn(PASSWORD, str(caught.exception))
            with self.assertRaises(CredentialError) as caught:
                self.store.load("admin")
            self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertFalse(any(call[0] == "secret-read" for call in self.calls))

    def test_partial_provider_update_fails_closed_on_payload_attribute_mismatch(self):
        self.store.add_web("github", "https://example.test/login", USERNAME, PASSWORD)

        def failed_metadata(attributes):
            raise RuntimeError(PASSWORD)

        self.items[0].set_attributes = failed_metadata
        with self.assertRaises(CredentialError) as caught:
            self.store.add_web("github", "https://new.example.test/login", USERNAME, PASSWORD, replace=True)
        self.assertNotIn(PASSWORD, str(caught.exception))
        with self.assertRaises(CredentialError):
            self.store.load("github")

    def test_missing_or_locked_collection_never_creates_or_unlocks(self):
        self.locked = True
        self.assertEqual(self.store.status(), {"available": True, "locked": True})
        with self.assertRaises(CredentialError):
            self.store.list_profiles()
        self.assertFalse(any(call[0] == "create" for call in self.calls))

        def missing(connection, alias):
            raise RuntimeError(PASSWORD)

        self.module.get_collection_by_alias = missing
        self.assertEqual(self.store.status(), {"available": False, "locked": None})
        with self.assertRaises(CredentialError) as caught:
            self.store.add_sudo("admin", PASSWORD)
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_duplicate_native_aliases_fail_closed(self):
        self.store.add_web("github", "https://example.test/login", USERNAME, PASSWORD)
        self.items.append(self.Item(self.items[0].attributes, self.items[0].secret))
        with self.assertRaises(CredentialError):
            self.store.load("github")
        self.assertFalse(any(call[0] == "secret-read" for call in self.calls))


class CredentialConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock_path = self.root / "private" / ".store.lock"
        self.backend_path = self.root / "synthetic-keyring.json"

    def test_shared_fake_adapter_thread_mutations_preserve_every_approval(self):
        store = CredentialStore(FakeBackend())
        self.assertIsNone(store.lock_path)
        store.add_sudo("admin", PASSWORD)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda index: store.allow_sudo("admin", "action-" + str(index), ["/usr/bin/id"]), range(16)))
        self.assertEqual(len(results), 16)
        self.assertEqual(len(store.load("admin")["actions"]), 16)

    def test_subprocess_add_collisions_and_approvals_are_serialized(self):
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
            added = list(executor.map(credential_process_worker,
                         [(str(self.backend_path), str(self.lock_path), "add", "") for _ in range(4)]))
            self.assertEqual(added.count("created"), 1)
            self.assertEqual(added.count("collision"), 3)
            approved = list(executor.map(credential_process_worker,
                            [(str(self.backend_path), str(self.lock_path), "approve", "action-" + str(index)) for index in range(12)]))
            self.assertEqual(approved, ["approved"] * 12)
            revoked = list(executor.map(credential_process_worker,
                           [(str(self.backend_path), str(self.lock_path), "revoke", "action-" + str(index)) for index in range(6)]))
            self.assertEqual(revoked, [True] * 6)
        store = CredentialStore(FileBackend(self.backend_path), lock_path=self.lock_path)
        self.assertEqual(set(store.load("admin")["actions"]), {"action-" + str(index) for index in range(6, 12)})
        self.assertEqual(self.lock_path.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(self.lock_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.lock_path.parent.stat().st_mode), 0o700)

    def test_lock_timeout_is_bounded_and_read_paths_participate(self):
        store = CredentialStore(FakeBackend(), lock_path=self.lock_path, lock_timeout_seconds=0.05)
        store.list_profiles()
        descriptor = os.open(self.lock_path, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with self.assertRaises(CredentialError) as caught:
            store.list_profiles()
        self.assertIn("busy", str(caught.exception))
        self.assertLess(time.monotonic() - started, 0.5)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertEqual(store.list_profiles(), [])

    def test_symbolic_hard_link_and_nonregular_lock_files_are_rejected(self):
        self.lock_path.parent.mkdir(mode=0o700)
        target = self.root / "target"
        target.write_text("preserve this content")
        target.chmod(0o640)
        self.lock_path.symlink_to(target)
        store = CredentialStore(FakeBackend(), lock_path=self.lock_path)
        with self.assertRaises(CredentialError):
            store.list_profiles()
        self.lock_path.unlink()
        os.link(target, self.lock_path)
        with self.assertRaises(CredentialError):
            store.list_profiles()
        self.assertEqual(target.read_text(), "preserve this content")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.lock_path.unlink()
        os.mkfifo(self.lock_path)
        with self.assertRaises(CredentialError):
            store.list_profiles()

    def test_symbolic_private_directory_is_rejected(self):
        alternate = self.root / "alternate"
        alternate.mkdir()
        self.lock_path.parent.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(CredentialError):
            CredentialStore(FakeBackend(), lock_path=self.lock_path).list_profiles()
        self.assertEqual(list(alternate.iterdir()), [])

    def test_invalid_timeouts_are_rejected_without_storage_access(self):
        for value in (False, None, "short", 0, -1, 61, float("nan"), float("inf")):
            with self.subTest(value=repr(value)), self.assertRaises(CredentialError):
                CredentialStore(FakeBackend(), lock_timeout_seconds=value)


if __name__ == "__main__":
    unittest.main()
