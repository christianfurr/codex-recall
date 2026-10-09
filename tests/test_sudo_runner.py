"""Synthetic sudo stand-ins only: these tests never invoke real sudo or root."""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_memory import sudo_runner
from codex_memory.sudo_runner import SudoError, validate_action


SYNTHETIC_PASSWORD = "synthetic-only-sudo-password-🍋-12345"
SYNTHETIC_HASH = hashlib.sha256((SYNTHETIC_PASSWORD + "\n").encode()).hexdigest()

_STUB = r'''#!/usr/bin/python3
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

expected_hash = "EXPECTED_HASH"
assert sys.argv[1:5] == ["-A", "-p", "", "--"]
mode, report_name = sys.argv[-2:]
helper = Path(os.environ["SUDO_ASKPASS"])
directory = helper.parent
pipe = directory / "password.pipe"
report = {
    "args": sys.argv[1:],
    "env_keys": sorted(os.environ),
    "stdin_empty": sys.stdin.buffer.read() == b"",
    "directory_mode": stat.S_IMODE(directory.stat().st_mode),
    "helper_mode": stat.S_IMODE(helper.stat().st_mode),
    "fifo_mode": stat.S_IMODE(pipe.stat().st_mode),
    "fifo_is_fifo": stat.S_ISFIFO(pipe.stat().st_mode),
    "temporary_directory": str(directory),
}
if mode == "cached":
    report["helper_invoked"] = False
elif mode == "timeout":
    child = subprocess.Popen(["/usr/bin/sleep", "30"])
    report["child_pid"] = child.pid
    Path(report_name).write_text(json.dumps(report))
    def finish(signum, frame):
        child.wait(timeout=2)
        sys.exit(128 + signum)
    signal.signal(signal.SIGTERM, finish)
    time.sleep(30)
elif mode == "broken-helper":
    helper.unlink()
    report["helper_invoked"] = False
    Path(report_name).write_text(json.dumps(report))
    sys.exit(1)
else:
    first = subprocess.run([str(helper), "ignored prompt"], capture_output=True, timeout=3)
    password = first.stdout
    report["helper_invoked"] = True
    report["password_correct"] = hashlib.sha256(password).hexdigest() == expected_hash
    report["password_in_args"] = password.rstrip(b"\n").decode() in str(sys.argv)
    report["password_in_env"] = password.rstrip(b"\n").decode() in str(dict(os.environ))
    report["password_in_regular_files"] = any(
        password.rstrip(b"\n") in entry.read_bytes()
        for entry in directory.iterdir() if entry.is_file()
    )
    report["marker_mode"] = stat.S_IMODE((directory / "used").stat().st_mode)
    report["marker_size"] = (directory / "used").stat().st_size
    started = time.monotonic()
    second = subprocess.run([str(helper)], capture_output=True, timeout=3)
    report["second_returncode"] = second.returncode
    report["second_output_empty"] = second.stdout == b""
    report["second_elapsed"] = time.monotonic() - started
    # Deliberately emit the synthetic password: the runner must discard both.
    sys.stdout.buffer.write(password)
    sys.stderr.buffer.write(password)
Path(report_name).write_text(json.dumps(report))
sys.exit(17 if mode == "failure" else 0)
'''


class SudoRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.stub = self.directory / "synthetic-sudo"
        self.stub.write_text(_STUB.replace("EXPECTED_HASH", SYNTHETIC_HASH))
        self.stub.chmod(0o700)
        self.report_path = self.directory / "proof.json"

    def profile(self, mode="success"):
        return {
            "alias": "synthetic-admin",
            "kind": "sudo",
            "password": SYNTHETIC_PASSWORD,
            "actions": {"safe-check": ["/usr/bin/id", mode, str(self.report_path)]},
        }

    def run_stub(self, mode="success", *, timeout=3, cancel_event=None):
        def validate(path):
            if path == str(self.stub):
                return path
            return sudo_runner._protected_executable(path)
        return sudo_runner._run_sudo(
            self.profile(mode), "safe-check", timeout,
            cancel_event=cancel_event, sudo_path=str(self.stub), executable_validator=validate,
        )

    def proof(self):
        return json.loads(self.report_path.read_text())

    def assert_cleaned_up(self):
        self.assertFalse(Path(self.proof()["temporary_directory"]).exists())
        self.assertFalse(any(thread.name == "codex-recall-askpass" for thread in threading.enumerate()))

    def test_password_is_only_delivered_once_through_a_private_fifo(self):
        with patch.dict(os.environ, {"UNRELATED_PRIVATE_VALUE": SYNTHETIC_PASSWORD}):
            result = self.run_stub()
        self.assertEqual(result, {
            "alias": "synthetic-admin", "action": "safe-check", "status": "succeeded", "exit_code": 0,
        })
        proof = self.proof()
        self.assertTrue(proof["password_correct"])
        self.assertTrue(proof["stdin_empty"])
        self.assertFalse(proof["password_in_args"])
        self.assertFalse(proof["password_in_env"])
        self.assertFalse(proof["password_in_regular_files"])
        self.assertEqual(proof["env_keys"], ["LC_ALL", "PATH", "SUDO_ASKPASS"])
        self.assertEqual(proof["directory_mode"], 0o700)
        self.assertEqual(proof["helper_mode"], 0o700)
        self.assertEqual(proof["fifo_mode"], 0o600)
        self.assertTrue(proof["fifo_is_fifo"])
        self.assertEqual(proof["marker_mode"], 0o600)
        self.assertEqual(proof["marker_size"], 0)
        self.assertNotEqual(proof["second_returncode"], 0)
        self.assertTrue(proof["second_output_empty"])
        self.assertLess(proof["second_elapsed"], 1)
        self.assertNotIn(SYNTHETIC_PASSWORD, json.dumps(result))
        self.assert_cleaned_up()

    def test_failed_sudo_only_returns_exit_code_and_safe_labels(self):
        result = self.run_stub("failure")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 17)
        self.assertEqual(set(result), {"alias", "action", "status", "exit_code"})
        self.assertNotIn(SYNTHETIC_PASSWORD, json.dumps(result))
        self.assert_cleaned_up()

    def test_cached_or_passwordless_sudo_never_feeds_the_command(self):
        started = time.monotonic()
        result = self.run_stub("cached")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(self.proof()["stdin_empty"])
        self.assertFalse(self.proof()["helper_invoked"])
        self.assertLess(time.monotonic() - started, 2)
        self.assert_cleaned_up()

    def test_helper_failure_cancels_the_fifo_writer(self):
        result = self.run_stub("broken-helper")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 1)
        self.assert_cleaned_up()

    def test_timeout_kills_the_process_group_and_cleans_up(self):
        result = self.run_stub("timeout", timeout=1)
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNone(result["exit_code"])
        child_pid = self.proof()["child_pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assert_cleaned_up()

    def test_cancel_before_launch_executes_nothing(self):
        canceled = threading.Event()
        canceled.set()
        with patch.object(sudo_runner.subprocess, "Popen") as launch:
            result = self.run_stub(cancel_event=canceled)
        self.assertEqual(result, {
            "alias": "synthetic-admin", "action": "safe-check", "status": "canceled", "exit_code": None,
        })
        launch.assert_not_called()
        self.assertFalse(self.report_path.exists())
        self.assertFalse(any(thread.name == "codex-recall-askpass" for thread in threading.enumerate()))

    def test_cancel_stops_child_group_writer_and_private_temporary_directory(self):
        canceled = threading.Event()
        results = []
        failures = []

        def run():
            try:
                results.append(self.run_stub("timeout", timeout=30, cancel_event=canceled))
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=run, name="synthetic-sudo-test")
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while not self.report_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.report_path.exists(), "Synthetic child did not initialize")
            started = time.monotonic()
            canceled.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(failures)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(results, [{
                "alias": "synthetic-admin", "action": "safe-check", "status": "canceled", "exit_code": None,
            }])
            with self.assertRaises(ProcessLookupError):
                os.kill(self.proof()["child_pid"], 0)
            self.assert_cleaned_up()
        finally:
            canceled.set()
            worker.join(timeout=3)

    def test_unknown_action_is_rejected_before_inspecting_password(self):
        class Profile(dict):
            def get(self, key, default=None):
                if key == "password":
                    raise AssertionError("Password must not be consulted")
                return super().get(key, default)
        with self.assertRaises(SudoError) as caught:
            sudo_runner.run_sudo(Profile(self.profile()), "unapproved-action")
        self.assertEqual(str(caught.exception), "The approved sudo action is unavailable.")
        self.assertNotIn("unapproved-action", str(caught.exception))

    def test_the_actual_saved_password_cannot_be_an_approved_literal_argument(self):
        for argument in (SYNTHETIC_PASSWORD, "--ordinary=" + SYNTHETIC_PASSWORD):
            profile = self.profile()
            profile["actions"]["safe-check"].append(argument)
            with patch.object(sudo_runner.subprocess, "Popen") as launch:
                with self.assertRaises(SudoError) as caught:
                    sudo_runner.run_sudo(profile, "safe-check")
            launch.assert_not_called()
            self.assertNotIn(SYNTHETIC_PASSWORD, str(caught.exception))

    def test_invalid_profiles_actions_passwords_and_timeouts_fail_safely(self):
        profiles = [None, {}, {**self.profile(), "kind": "login"}, {**self.profile(), "alias": "bad\nlabel"}]
        profiles += [{**self.profile(), "password": value} for value in (None, "", "bad\npassword", "bad\rpassword", "bad\x00password", "x" * 4097)]
        for profile in profiles:
            with self.subTest(profile_type=type(profile).__name__), self.assertRaises(SudoError) as caught:
                sudo_runner.run_sudo(profile, "safe-check")
            self.assertNotIn(SYNTHETIC_PASSWORD, str(caught.exception))
        for timeout in (True, 0, -1, 301, "3", 1.5):
            with self.subTest(timeout=timeout), self.assertRaises(SudoError):
                sudo_runner.run_sudo(self.profile(), "safe-check", timeout)
        for argv in ([], [None], ["/usr/bin/id", "bad\x00arg"], "/usr/bin/id", [""]):
            profile = self.profile()
            profile["actions"]["safe-check"] = argv
            with self.subTest(argv_type=type(argv).__name__), self.assertRaises(SudoError):
                sudo_runner.run_sudo(profile, "safe-check")

    def test_spawn_failure_does_not_expose_exception_values(self):
        failure = OSError(SYNTHETIC_PASSWORD)
        with patch.object(sudo_runner.subprocess, "Popen", side_effect=failure):
            with self.assertRaises(SudoError) as caught:
                self.run_stub()
        self.assertEqual(str(caught.exception), "The approved sudo action could not be completed.")
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse(any(thread.name == "codex-recall-askpass" for thread in threading.enumerate()))

    def test_interruption_terminates_the_child_and_joins_writer(self):
        processes = []
        original_popen = subprocess.Popen

        def interrupted_popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            original_wait = process.wait
            first = True

            def wait(*wait_args, **wait_kwargs):
                nonlocal first
                if first:
                    first = False
                    raise KeyboardInterrupt(SYNTHETIC_PASSWORD)
                return original_wait(*wait_args, **wait_kwargs)
            process.wait = wait
            return process

        with patch.object(sudo_runner.subprocess, "Popen", side_effect=interrupted_popen):
            with self.assertRaises(SudoError) as caught:
                self.run_stub("timeout")
        self.assertNotIn(SYNTHETIC_PASSWORD, str(caught.exception))
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(any(thread.name == "codex-recall-askpass" for thread in threading.enumerate()))

    def test_runtime_executable_validation_rejects_user_editable_files(self):
        mutable_alias = self.directory / "mutable-alias"
        mutable_alias.symlink_to("/usr/bin/id")
        for value in (str(self.stub), str(mutable_alias), str(self.directory / "missing"), "relative-executable", "/tmp"):
            with self.subTest(path=value), self.assertRaises(SudoError) as caught:
                validate_action([value])
            self.assertNotIn(value, str(caught.exception))
        resolved = validate_action(["/usr/bin/id", "--user"])
        self.assertEqual(resolved, [str(Path("/usr/bin/id").resolve()), "--user"])

    def test_interpreters_shells_and_nested_dispatchers_are_rejected(self):
        for name in ("sh", "bash", "python3.10", "env", "sudo", "xargs", "find", "busybox", "node", "lua5.4"):
            with self.subTest(name=name), self.assertRaises(SudoError):
                sudo_runner._reject_dispatcher("/usr/bin/" + name)
        with self.assertRaises(SudoError):
            validate_action(["/bin/sh", "-c", "synthetic action"])
        profile = self.profile()
        profile["actions"]["safe-check"] = ["/bin/sh", "-c", "synthetic action"]
        with self.assertRaises(SudoError):
            sudo_runner.run_sudo(profile, "safe-check")

    def test_runtime_executable_validation_rejects_writable_owner_or_parent(self):
        original_stat = Path.stat
        target = Path("/usr/bin/id").resolve()
        for unsafe in (target, target.parent):
            def fake_stat(path, *args, **kwargs):
                info = original_stat(path, *args, **kwargs)
                if path == unsafe:
                    values = list(info)
                    values[0] |= stat.S_IWGRP
                    return os.stat_result(values)
                return info
            with self.subTest(target=str(unsafe)), patch.object(Path, "stat", fake_stat):
                with self.assertRaises(SudoError):
                    validate_action([str(target)])


if __name__ == "__main__":
    unittest.main()
