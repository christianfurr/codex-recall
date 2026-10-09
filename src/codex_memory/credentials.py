"""Opt-in, namespaced login storage; secret values never enter memory tools.

The native adapter uses an existing unlocked Linux Secret Service collection.
It never creates or unlocks a collection. Other local programs running as the
same user may access that collection; this module is not a sandbox boundary.
"""

from __future__ import annotations

import ipaddress
import fcntl
import json
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import unquote, urlsplit, urlunsplit

from .security import reject_secrets, secure_directory


APPLICATION = "codex-recall"
SCHEMA = "1"
_PROVIDER_SCHEMA = "org.freedesktop.Secret.Generic"
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z", re.ASCII)
_ATTRIBUTES = {"application", "schema", "alias", "kind", "origin", "actions"}
_MAX_SECRET_BYTES = 1024 * 1024
_UNAVAILABLE = "Credential storage is unavailable. Configure an existing Linux Secret Service keyring."
_LOCKED = "Credential storage is locked. Unlock your keyring through the desktop before retrying."
_INVALID = "Credential profile is invalid. Remove and recreate it through the local login command."
_DUPLICATE = "Duplicate credential aliases exist. Resolve them through the keyring application."
_COLLISION = "That login alias already exists. Use explicit replacement or choose another alias."
_LOCK_FAILURE = "Credential storage could not be locked safely. Check private directory ownership and permissions."
_BUSY = "Credential storage is busy. Retry after the current login operation finishes."


class CredentialError(ValueError):
    """A fixed, sanitized credential failure; never include submitted values."""


def _no_recognizable_secrets(value: str) -> None:
    try:
        reject_secrets(value)
    except ValueError:
        raise CredentialError("Credential metadata cannot contain recognizable secrets.") from None


def validate_alias(value: object) -> str:
    """Validate public aliases and approved action names without echoing input."""
    if not isinstance(value, str) or not _ALIAS.fullmatch(value):
        raise CredentialError("Use a lowercase alias of 1–64 letters, digits, underscores or hyphens, starting with a letter.")
    _no_recognizable_secrets(value)
    return value


def normalize_login_url(value: object) -> tuple[str, str]:
    """Return a canonical HTTPS login URL and its exact scheme/host/port origin."""
    error = "Use an HTTPS login URL without credentials, query, fragment, whitespace or backslashes."
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
            or any(character in value for character in ("\\", "?", "#"))):
        raise CredentialError(error)
    _no_recognizable_secrets(value)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not parsed.hostname
                or "%" in parsed.netloc):
            raise ValueError
        host = parsed.hostname
        if ":" in host:
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            # Reject browser-dependent host spellings, trailing dots and
            # non-domain punctuation rather than binding ambiguous origins.
            if (len(host) > 253 or host.endswith(".")
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                           for part in host.split("."))):
                raise ValueError
            if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.rsplit(".", 1)[-1]):
                host = str(ipaddress.IPv4Address(host))
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        # Explicit empty ports and malformed bracket suffixes are ambiguous.
        suffix = parsed.netloc.rsplit("]", 1)[-1] if parsed.netloc.startswith("[") else parsed.netloc[len(parsed.hostname):]
        if suffix and not re.fullmatch(r":[0-9]+", suffix):
            raise ValueError
        authority = host + (":" + str(port) if port not in (None, 443) else "")
        path = parsed.path or "/"
        decoded = unquote(path, errors="strict")
        if (any(ord(character) < 32 or ord(character) == 127 for character in decoded)
                or "\\" in decoded):
            raise ValueError
        _no_recognizable_secrets(decoded)
        origin = "https://" + authority
        normalized = urlunsplit(("https", authority, path, "", ""))
        normalized.encode("utf-8")
        return normalized, origin
    except CredentialError:
        raise
    except (ValueError, UnicodeError):
        raise CredentialError(error) from None


def validate_command(command: object) -> list[str]:
    """Validate literal argv only; the execution adapter checks filesystem trust."""
    error = "Approve a fixed argument list with an absolute executable path and no secret values."
    if not isinstance(command, list) or not 1 <= len(command) <= 64:
        raise CredentialError(error)
    if (any(not isinstance(argument, str) or len(argument) > 4096
            or any(character in argument for character in ("\x00", "\r", "\n")) for argument in command)
            or not command[0] or not PurePosixPath(command[0]).is_absolute()
            or sum(len(argument) for argument in command) > 16384):
        raise CredentialError(error)
    for argument in command:
        _no_recognizable_secrets(argument)
    _no_recognizable_secrets(" ".join(command))
    # Common split-form credential flags would otherwise evade patterns that
    # recognize only assignments such as password=value.
    if any(re.fullmatch(r"--?(?:password|passwd|pwd|api[-_]key|token|secret|client[-_]secret)", argument, re.I)
           for argument in command):
        raise CredentialError(error)
    try:
        "".join(command).encode("utf-8")
    except UnicodeError:
        raise CredentialError(error) from None
    return list(command)


def _private_text(value: object, *, maximum: int, sudo: bool = False) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value
            or (sudo and any(character in value for character in ("\r", "\n")))):
        raise CredentialError("Enter a nonempty credential within the supported length; unsupported control characters are not accepted.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CredentialError("The credential contains unsupported text.") from None
    return value


def _actions(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict) or len(value) > 32:
        raise CredentialError(_INVALID)
    return {validate_alias(name): validate_command(command) for name, command in value.items()}


def _validate_payload(value: object) -> dict:
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1):
        raise CredentialError(_INVALID)
    alias = validate_alias(value.get("alias"))
    kind = value.get("kind")
    if kind == "web":
        if set(value) != {"version", "alias", "kind", "username", "password", "login_url", "origin", "actions"}:
            raise CredentialError(_INVALID)
        url, origin = normalize_login_url(value["login_url"])
        if value["login_url"] != url or value["origin"] != origin or value["actions"] != {}:
            raise CredentialError(_INVALID)
        username = _private_text(value["username"], maximum=1024)
        password = _private_text(value["password"], maximum=16384)
        return {"version": 1, "alias": alias, "kind": kind, "username": username,
                "password": password, "login_url": url, "origin": origin, "actions": {}}
    if kind == "sudo":
        if set(value) != {"version", "alias", "kind", "password", "actions"}:
            raise CredentialError(_INVALID)
        return {"version": 1, "alias": alias, "kind": kind,
                "password": _private_text(value["password"], maximum=1024, sudo=True),
                "actions": _actions(value["actions"])}
    raise CredentialError(_INVALID)


def _attributes(profile: dict) -> dict[str, str]:
    return {"application": APPLICATION, "schema": SCHEMA, "alias": profile["alias"],
            "kind": profile["kind"], "origin": profile.get("origin", ""),
            "actions": json.dumps(sorted(profile["actions"]), separators=(",", ":"))}


def _metadata(attributes: object) -> dict:
    if (not isinstance(attributes, dict) or set(attributes) != _ATTRIBUTES
            or any(not isinstance(value, str) or len(value) > 4096 for value in attributes.values())
            or attributes["application"] != APPLICATION or attributes["schema"] != SCHEMA):
        raise CredentialError(_INVALID)
    alias = validate_alias(attributes["alias"])
    try:
        names = json.loads(attributes["actions"])
    except (ValueError, TypeError, RecursionError):
        raise CredentialError(_INVALID) from None
    if (not isinstance(names, list) or len(names) > 32
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)):
        raise CredentialError(_INVALID)
    names = sorted(validate_alias(name) for name in names)
    if attributes["kind"] == "web":
        _, origin = normalize_login_url(attributes["origin"])
        if origin != attributes["origin"] or names:
            raise CredentialError(_INVALID)
    elif attributes["kind"] == "sudo":
        if attributes["origin"]:
            raise CredentialError(_INVALID)
        origin = None
    else:
        raise CredentialError(_INVALID)
    return {"alias": alias, "kind": attributes["kind"], "origin": origin, "actions": names}


def _unique_json(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError(_INVALID)
        result[key] = value
    return result


def default_lock_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home and Path(data_home).expanduser().is_absolute() else Path.home() / ".local" / "share"
    return base / "codex-recall-credentials" / ".store.lock"


def _serialized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._locked():
            return method(self, *args, **kwargs)
    return locked


class SecretServiceBackend:
    """Adapter API: status, list_entries, read, write and delete.

    ``list_entries`` returns attributes only. ``read`` returns an attributes/
    secret-bytes pair, and is used exclusively by local credential consumers.
    All lookups are constrained to this application's namespace and alias.
    """

    @contextmanager
    def _collection(self, *, permit_locked: bool = False) -> Iterator[object]:
        connection = None
        try:
            import secretstorage
            connection = secretstorage.dbus_init()
            collection = secretstorage.get_collection_by_alias(connection, "default")
            if not permit_locked and collection.is_locked():
                raise CredentialError(_LOCKED)
            yield collection
        except CredentialError:
            raise
        except Exception:
            raise CredentialError(_UNAVAILABLE) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def status(self) -> dict:
        with self._collection(permit_locked=True) as collection:
            return {"available": True, "locked": bool(collection.is_locked())}

    @staticmethod
    def _attributes(item: object) -> dict[str, str]:
        attributes = item.get_attributes()
        if isinstance(attributes, dict) and "xdg:schema" in attributes:
            # GNOME supplies this standard nonconfidential attribute even
            # when create_item omitted it. Do not ignore arbitrary provider
            # extras or schema values, which could conceal corrupt metadata.
            if attributes["xdg:schema"] != _PROVIDER_SCHEMA:
                raise CredentialError(_INVALID)
            attributes = {key: value for key, value in attributes.items() if key != "xdg:schema"}
        _metadata(attributes)
        return attributes

    def list_entries(self) -> list[dict[str, str]]:
        with self._collection() as collection:
            return [self._attributes(item) for item in collection.search_items({"application": APPLICATION})]

    @staticmethod
    def _item(collection: object, alias: str) -> object | None:
        items = list(collection.search_items({"application": APPLICATION, "alias": alias}))
        if len(items) > 1:
            raise CredentialError(_DUPLICATE)
        return items[0] if items else None

    def read(self, alias: str) -> tuple[dict[str, str], bytes] | None:
        with self._collection() as collection:
            item = self._item(collection, alias)
            if item is None:
                return None
            if item.is_locked():
                raise CredentialError(_LOCKED)
            return self._attributes(item), item.get_secret()

    def write(self, alias: str, attributes: dict[str, str], secret: bytes, *, replace: bool = False) -> None:
        with self._collection() as collection:
            item = self._item(collection, alias)
            provider_attributes = {**attributes, "xdg:schema": _PROVIDER_SCHEMA}
            if item is None:
                collection.create_item("Codex Recall login: " + alias, provider_attributes, secret,
                                       replace=False, content_type="application/json")
                return
            if not replace:
                raise CredentialError(_COLLISION)
            if item.is_locked():
                raise CredentialError(_LOCKED)
            # These provider calls are not atomic. Payload/attribute agreement
            # is checked on every load, so a partial metadata update fails shut.
            item.set_secret(secret, content_type="application/json")
            item.set_attributes(provider_attributes)

    def delete(self, alias: str) -> bool:
        with self._collection() as collection:
            item = self._item(collection, alias)
            if item is None:
                return False
            if item.is_locked():
                raise CredentialError(_LOCKED)
            item.delete()
            return True


class CredentialStore:
    """Safe public metadata plus private local consumers, outside the memory DB."""

    def __init__(self, backend: object | None = None, *, lock_path: str | Path | None = None,
                 lock_timeout_seconds: float = 5.0) -> None:
        if (isinstance(lock_timeout_seconds, bool) or not isinstance(lock_timeout_seconds, (int, float))
                or not 0 < lock_timeout_seconds <= 60):
            raise CredentialError("Credential lock timeout must be between zero and sixty seconds.")
        self.backend = SecretServiceBackend() if backend is None else backend
        self.lock_path = (Path(lock_path).expanduser().absolute() if lock_path is not None
                          else default_lock_path() if isinstance(self.backend, SecretServiceBackend) else None)
        self.lock_timeout_seconds = lock_timeout_seconds
        self._thread_lock = threading.RLock()
        self._lock_depth = threading.local()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize complete read/modify/write operations, including nested calls.

        The lock contains no data and coordinates Codex Recall processes only.
        Direct changes from the desktop keyring UI cannot honor this lock.
        Injected adapters use an in-process lock unless a test lock is supplied.
        """
        if not self._thread_lock.acquire(timeout=self.lock_timeout_seconds):
            raise CredentialError(_BUSY)
        depth = getattr(self._lock_depth, "value", 0)
        self._lock_depth.value = depth + 1
        descriptor = None
        try:
            if depth == 0 and self.lock_path is not None:
                try:
                    secure_directory(self.lock_path.parent)
                    descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or info.st_nlink != 1):
                        raise CredentialError(_LOCK_FAILURE)
                    os.fchmod(descriptor, 0o600)
                    deadline = time.monotonic() + self.lock_timeout_seconds
                    while True:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise CredentialError(_BUSY) from None
                            time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
                except CredentialError:
                    raise
                except (ValueError, OSError):
                    raise CredentialError(_LOCK_FAILURE) from None
            yield
        finally:
            if descriptor is not None:
                # Closing the descriptor releases flock even after failures.
                os.close(descriptor)
            self._lock_depth.value = depth
            self._thread_lock.release()

    def _call(self, operation: str, *args: object, **kwargs: object) -> object:
        try:
            return getattr(self.backend, operation)(*args, **kwargs)
        except CredentialError as error:
            # Even an adapter using our public error type could pass a raw
            # provider exception as its message. Only known fixed messages may
            # cross this boundary.
            message = str(error)
            raise CredentialError(message if message in (_UNAVAILABLE, _LOCKED, _DUPLICATE, _COLLISION) else _UNAVAILABLE) from None
        except Exception:
            # Backend errors can include secrets; never stringify or chain them.
            raise CredentialError(_UNAVAILABLE) from None

    @_serialized
    def status(self) -> dict:
        try:
            result = self._call("status")
            if (not isinstance(result, dict) or type(result.get("available")) is not bool
                    or (result["available"] and type(result.get("locked")) is not bool)):
                raise CredentialError(_UNAVAILABLE)
            return {"available": result["available"], "locked": result["locked"] if result["available"] else None}
        except CredentialError:
            return {"available": False, "locked": None}

    @_serialized
    def list_profiles(self) -> list[dict]:
        entries = self._call("list_entries")
        if not isinstance(entries, list):
            raise CredentialError(_INVALID)
        result = []
        seen = set()
        for attributes in entries:
            profile = _metadata(attributes)
            if profile["alias"] in seen:
                raise CredentialError(_DUPLICATE)
            seen.add(profile["alias"])
            result.append(profile)
        return sorted(result, key=lambda profile: profile["alias"])

    @_serialized
    def load(self, alias: str) -> dict:
        """PRIVATE: validated secret payload for local adapters, never MCP output."""
        alias = validate_alias(alias)
        entry = self._call("read", alias)
        if entry is None:
            raise CredentialError("The requested login alias does not exist.")
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise CredentialError(_INVALID)
        attributes, raw = entry
        _metadata(attributes)
        if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_SECRET_BYTES:
            raise CredentialError(_INVALID)
        try:
            profile = _validate_payload(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json))
        except (ValueError, UnicodeError, RecursionError):
            raise CredentialError(_INVALID) from None
        if profile["alias"] != alias or attributes != _attributes(profile):
            raise CredentialError(_INVALID)
        return profile

    def _save(self, profile: dict, *, replace: bool) -> dict:
        if type(replace) is not bool:
            raise CredentialError("Replacement must be explicitly enabled or disabled.")
        profile = _validate_payload(profile)
        attributes = _attributes(profile)
        raw = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > _MAX_SECRET_BYTES:
            raise CredentialError(_INVALID)
        self._call("write", profile["alias"], attributes, raw, replace=replace)
        return _metadata(attributes)

    def _existing(self, alias: str, kind: str, replace: bool) -> dict | None:
        if type(replace) is not bool:
            raise CredentialError("Replacement must be explicitly enabled or disabled.")
        # Metadata discovery does not retrieve existing credentials unless an
        # explicit same-kind replacement needs the stored sudo approvals.
        existing = next((profile for profile in self.list_profiles() if profile["alias"] == alias), None)
        if existing is not None:
            if not replace:
                raise CredentialError(_COLLISION)
            if existing["kind"] != kind:
                raise CredentialError("The alias belongs to another login type. Remove it before changing its type.")
        return existing

    @_serialized
    def add_web(self, alias: str, login_url: str, username: str, password: str, replace: bool = False) -> dict:
        alias = validate_alias(alias)
        url, origin = normalize_login_url(login_url)
        username = _private_text(username, maximum=1024)
        password = _private_text(password, maximum=16384)
        self._existing(alias, "web", replace)
        return self._save({"version": 1, "alias": alias, "kind": "web", "username": username,
                           "password": password, "login_url": url, "origin": origin, "actions": {}}, replace=replace)

    @_serialized
    def add_sudo(self, alias: str, password: str, replace: bool = False) -> dict:
        alias = validate_alias(alias)
        password = _private_text(password, maximum=1024, sudo=True)
        existing = self._existing(alias, "sudo", replace)
        actions = self.load(alias)["actions"] if existing is not None else {}
        return self._save({"version": 1, "alias": alias, "kind": "sudo", "password": password,
                           "actions": actions}, replace=replace)

    @_serialized
    def allow_sudo(self, alias: str, action: str, command: list[str], replace: bool = False) -> dict:
        alias, action = validate_alias(alias), validate_alias(action)
        command = validate_command(command)
        if type(replace) is not bool:
            raise CredentialError("Replacement must be explicitly enabled or disabled.")
        profile = self.load(alias)
        if profile["kind"] != "sudo":
            raise CredentialError("Approved administrator actions require a sudo login profile.")
        if action in profile["actions"] and not replace:
            raise CredentialError("That action is already approved. Use explicit replacement to change its command.")
        profile["actions"][action] = command
        return self._save(profile, replace=True)

    @_serialized
    def remove(self, alias: str) -> dict:
        alias = validate_alias(alias)
        deleted = self._call("delete", alias)
        if type(deleted) is not bool:
            raise CredentialError(_UNAVAILABLE)
        return {"alias": alias, "deleted": deleted}

    @_serialized
    def revoke_sudo(self, alias: str, action: str) -> bool:
        """Retract one approved action without deleting the local credential."""
        alias, action = validate_alias(alias), validate_alias(action)
        profile = self.load(alias)
        if profile["kind"] != "sudo":
            raise CredentialError("Approved administrator actions require a sudo login profile.")
        if action not in profile["actions"]:
            return False
        del profile["actions"][action]
        self._save(profile, replace=True)
        return True
