"""Run an explicitly approved sudo action without returning its secret or output.

Passwords travel through a private, one-use FIFO to sudo's askpass helper. The
command's standard input is always disconnected; a cached sudo ticket cannot
turn the password into command input. This module does not change sudo policy
or create, invalidate, or refresh tickets separately from the approved action.
"""

from __future__ import annotations

import errno
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .credentials import CredentialError, validate_alias, validate_command


class SudoError(ValueError):
    """A fixed, safe error with no credential or command details."""


_SUDO_PATH = "/usr/bin/sudo"
_CAT_PATH = "/usr/bin/cat"
_SHELL_PATH = "/bin/sh"
_MAX_PASSWORD_BYTES = 4096
_DISPATCHERS = frozenset({
    "sh", "dash", "bash", "ash", "zsh", "ksh", "fish", "csh", "tcsh", "pwsh", "powershell",
    "python", "pypy", "perl", "ruby", "php", "node", "nodejs", "lua", "r", "rscript", "julia",
    "awk", "gawk", "mawk", "nawk", "tclsh", "wish", "js", "qjs", "deno", "bun", "java",
    "dotnet", "mono", "go", "gcc", "cc", "g++", "clang", "clang++", "make", "cmake", "cargo",
    "rustc", "swift", "sudo", "su", "doas", "pkexec", "runuser", "setpriv", "nsenter", "chroot",
    "env", "xargs", "timeout", "setsid", "nohup", "nice", "ionice", "stdbuf", "watch", "parallel",
    "command", "exec", "start-stop-daemon", "busybox", "toybox", "find", "at", "batch",
})


def validate_action(argv: object) -> list[str]:
    """Validate a fixed argv and resolve its protected, root-owned executable.

    The executable and every resolved parent must be owned by root and cannot
    be writable by a group or other users. This prevents a user replacing the
    executable after approving its path. Arguments remain literal argv entries.
    Approval of their meaning belongs to the interactive registration flow.
    """
    argv = _literal_argv(argv)
    executable = _protected_executable(argv[0])
    _reject_dispatcher(executable)
    return [executable, *argv[1:]]


def _reject_dispatcher(executable: str) -> None:
    name = Path(executable).name.lower()
    if name in _DISPATCHERS or re.fullmatch(r"(?:python|pypy|lua|ruby|php|perl|tclsh|wish)[0-9.]*", name):
        raise SudoError("Shells, interpreters, and command dispatchers are not supported sudo actions.")


def _literal_argv(argv: object) -> list[str]:
    try:
        return validate_command(argv)
    except CredentialError:
        raise SudoError("The approved sudo action is invalid.") from None


def _protected_executable(value: str) -> str:
    try:
        supplied = Path(value)
        if not supplied.is_absolute():
            raise ValueError
        # A mutable alias could be retargeted between approval and use. Normal
        # root-owned links such as /bin -> /usr/bin remain supported.
        supplied_info = supplied.lstat()
        if supplied_info.st_uid != 0:
            raise ValueError
        for parent in supplied.parents:
            info = parent.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError
        path = supplied.resolve(strict=True)
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or not info.st_mode & 0o111
        ):
            raise ValueError
        for parent in path.parents:
            info = parent.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError
        return str(path)
    except Exception:
        raise SudoError("The sudo executable must be protected and owned by root.") from None


def _profile_action(profile: object, action: object, timeout: object) -> tuple[str, str, list[str], bytes]:
    if not isinstance(profile, dict) or profile.get("kind") != "sudo":
        raise SudoError("A sudo credential profile is required.")
    try:
        alias = validate_alias(profile.get("alias"))
    except CredentialError:
        raise SudoError("The sudo credential profile is invalid.") from None
    try:
        action = validate_alias(action)
    except CredentialError:
        raise SudoError("The approved sudo action is unavailable.") from None
    actions = profile.get("actions")
    if not isinstance(actions, dict) or action not in actions:
        raise SudoError("The approved sudo action is unavailable.")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise SudoError("The sudo timeout must be between 1 and 300 seconds.")
    # Validate the shape before looking at the password. Executable ownership
    # is checked separately, immediately before launching the process.
    argv = _literal_argv(actions[action])
    password = profile.get("password")
    if not isinstance(password, str) or not password or any(char in password for char in ("\x00", "\r", "\n")):
        raise SudoError("The sudo credential profile is invalid.")
    # A bare positional password can evade general recognizable-secret rules.
    # Reject the actual saved value even if it was accidentally approved as an
    # ordinary argument. No submitted argument or password enters the error.
    if any(password in argument for argument in argv):
        raise SudoError("The approved sudo action is invalid.")
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        raise SudoError("The sudo credential profile is invalid.") from None
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise SudoError("The sudo credential profile is invalid.")
    return alias, action, list(argv), encoded + b"\n"


def _feed_once(fifo: Path, password: bytes, stop: threading.Event, deadline: float) -> None:
    """A nonblocking writer remains cancellable when sudo never asks."""
    descriptor = None
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                break
            except OSError as error:
                if error.errno != errno.ENXIO:
                    return
                stop.wait(0.01)
        if descriptor is None:
            return
        remaining = memoryview(password)
        while remaining and not stop.is_set() and time.monotonic() < deadline:
            try:
                written = os.write(descriptor, remaining)
                if written == 0:
                    return
                remaining = remaining[written:]
            except BlockingIOError:
                stop.wait(0.01)
            except OSError:
                return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _terminate(process: subprocess.Popen) -> None:
    """Signal the sudo monitor and its group, then reap our child."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def run_sudo(
    profile: dict,
    action: str,
    timeout: int = 60,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Execute one saved action and return only labels, status, and exit code."""
    return _run_sudo(profile, action, timeout, cancel_event=cancel_event)


def _run_sudo(
    profile: dict,
    action: str,
    timeout: int = 60,
    *,
    cancel_event: threading.Event | None = None,
    sudo_path: str = _SUDO_PATH,
    executable_validator: Callable[[str], str] = _protected_executable,
) -> dict:
    """Internal injection points permit synthetic tests without invoking sudo."""
    process = None
    writer = None
    stop = threading.Event()
    try:
        alias, action, argv, password = _profile_action(profile, action, timeout)
        if cancel_event is not None and not isinstance(cancel_event, threading.Event):
            raise SudoError("The sudo cancellation request is invalid.")
        if cancel_event is not None and cancel_event.is_set():
            return {"alias": alias, "action": action, "status": "canceled", "exit_code": None}
        argv[0] = executable_validator(argv[0])
        _reject_dispatcher(argv[0])
        sudo_path = executable_validator(sudo_path)
        cat_path = executable_validator(_CAT_PATH)
        shell_path = executable_validator(_SHELL_PATH)
        with tempfile.TemporaryDirectory(prefix="codex-recall-sudo-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            fifo = directory / "password.pipe"
            os.mkfifo(fifo, 0o600)
            helper = directory / "askpass"
            # noclobber makes retries fail without opening the FIFO again. The
            # marker and script contain no credential; the FIFO is never a
            # regular file and carries the password only while being read.
            script = (
                f"#!{shell_path}\n"
                "umask 077\nset -C\n"
                f": > {shlex.quote(str(directory / 'used'))} 2>/dev/null || exit 1\n"
                f"exec {shlex.quote(cat_path)} -- {shlex.quote(str(fifo))}\n"
            )
            descriptor = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(script)
            writer = threading.Thread(
                target=_feed_once,
                args=(fifo, password, stop, time.monotonic() + timeout),
                name="codex-recall-askpass",
                daemon=False,
            )
            try:
                if cancel_event is not None and cancel_event.is_set():
                    return {"alias": alias, "action": action, "status": "canceled", "exit_code": None}
                process = subprocess.Popen(
                    [sudo_path, "-A", "-p", "", "--", *argv],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={"SUDO_ASKPASS": str(helper), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                    cwd="/",
                    close_fds=True,
                    start_new_session=True,
                )
                writer.start()
                deadline = time.monotonic() + timeout
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        _terminate(process)
                        exit_code = None
                        status = "canceled"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _terminate(process)
                        exit_code = None
                        status = "timed_out"
                        break
                    try:
                        exit_code = process.wait(timeout=min(0.1, remaining))
                        status = "succeeded" if exit_code == 0 else "failed"
                        break
                    except subprocess.TimeoutExpired:
                        continue
                return {"alias": alias, "action": action, "status": status, "exit_code": exit_code}
            finally:
                stop.set()
                try:
                    if process is not None:
                        _terminate(process)
                finally:
                    if writer.ident is not None:
                        writer.join()
    except SudoError:
        raise
    except BaseException:
        raise SudoError("The approved sudo action could not be completed.") from None
