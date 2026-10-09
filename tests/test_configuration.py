"""Configuration preservation and reversible, idempotent registration."""
import tempfile
import unittest
from pathlib import Path

from codex_memory.configure import edit, parse


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        (self.app / ".venv/bin").mkdir(parents=True)
        (self.app / ".venv/bin/python").touch()
        self.home = self.root / "codex"
        self.home.mkdir()
        self.state = self.root / "backups"
        self.db = self.root / "data/memory.db"
        self.config = b'# existing comment\nmodel = "example"\n[features]\nflag = true\n[mcp_servers.existing]\ncommand = "/example"\nargs = ["safe"]\n'
        (self.home / "config.toml").write_bytes(self.config)
        self.instructions = b'# Owner rules\nPreserve my instructions.\n'
        (self.home / "AGENTS.md").write_bytes(self.instructions)

    def invoke(self, action):
        return edit(action, self.app, self.home, self.state, self.db)

    def test_preservation_idempotence_backup_and_uninstall(self):
        result = self.invoke("install")
        self.assertEqual(result["changed"], 2)
        private = Path(result["backup_directory"])
        self.assertEqual((private / "config.toml").read_bytes(), self.config)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual((private / "config.toml").stat().st_mode & 0o777, 0o600)
        installed = (self.home / "config.toml").read_bytes()
        self.assertTrue(installed.startswith(self.config))
        parsed = parse(installed)
        self.assertEqual(parsed["mcp_servers"]["existing"], parse(self.config)["mcp_servers"]["existing"])
        self.assertEqual(self.invoke("install")["changed"], 0)
        self.assertEqual(installed, (self.home / "config.toml").read_bytes())
        self.invoke("uninstall")
        self.assertEqual((self.home / "config.toml").read_bytes(), self.config)
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), self.instructions)
        self.assertEqual(self.invoke("uninstall")["changed"], 0)

    def test_uninstall_preserves_later_changes(self):
        self.invoke("install")
        f = self.home / "config.toml"
        f.write_bytes(f.read_bytes() + b'\n[later]\nnew_setting = 42\n')
        self.invoke("uninstall")
        self.assertEqual(parse(f.read_bytes())["later"]["new_setting"], 42)

    def test_nonempty_global_override_receives_guidance(self):
        override = self.home / "AGENTS.override.md"
        override.write_bytes(b'Override rules.\n')
        self.invoke("install")
        self.assertIn("CODEX RECALL", override.read_text())
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), self.instructions)

    def test_unmanaged_collision_refused(self):
        (self.home / "config.toml").write_bytes(self.config + b'[mcp_servers.local_memory]\ncommand = "other"\n')
        with self.assertRaises(ValueError):
            self.invoke("install")

    def test_invalid_toml_not_modified(self):
        f = self.home / "config.toml"
        f.write_bytes(b'bad = [')
        with self.assertRaises(ValueError):
            self.invoke("install")
        self.assertEqual(f.read_bytes(), b'bad = [')

    def test_symlink_config_refused(self):
        f = self.home / "config.toml"
        f.unlink()
        f.symlink_to(self.root / "target")
        with self.assertRaises(ValueError):
            self.invoke("install")

    def test_override_added_after_install_does_not_leave_stale_guidance(self):
        self.invoke("install")
        override = self.home / "AGENTS.override.md"
        override.write_bytes(b'New owner rules.\n')
        result = self.invoke("uninstall")
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), self.instructions)
        self.assertEqual(override.read_bytes(), b'New owner rules.\n')
        self.assertEqual(result["registered_database"], str(self.db))

    def test_reinstall_moves_guidance_to_active_override(self):
        self.invoke("install")
        override = self.home / "AGENTS.override.md"
        override.write_bytes(b'New owner rules.\n')
        self.invoke("install")
        self.assertNotIn("CODEX RECALL", (self.home / "AGENTS.md").read_text())
        self.assertEqual(override.read_text().count("BEGIN CODEX RECALL"), 1)
        self.invoke("uninstall")
        self.assertEqual(override.read_bytes(), b'New owner rules.\n')

    def test_saved_logins_are_opt_in_preserved_on_rerun_and_removed_on_uninstall(self):
        self.invoke("install")
        self.assertNotIn("local_credentials", parse((self.home / "config.toml").read_bytes())["mcp_servers"])
        edit("install", self.app, self.home, self.state, self.db, credentials=True)
        installed = parse((self.home / "config.toml").read_bytes())
        self.assertEqual(installed["mcp_servers"]["local_credentials"]["args"], ["-m", "codex_memory.credentials_server"])
        self.assertEqual(installed["mcp_servers"]["local_credentials"]["env_vars"],
                         ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_DATA_HOME", "CHROME_DEVEL_SANDBOX"])
        self.assertEqual(installed["mcp_servers"]["local_credentials"]["tool_timeout_sec"], 90)
        self.assertIn("Saved logins", (self.home / "AGENTS.md").read_text())
        self.assertEqual(self.invoke("install")["changed"], 0)
        self.assertIn("local_credentials", parse((self.home / "config.toml").read_bytes())["mcp_servers"])
        result = edit("install", self.app, self.home, self.state, self.db, credentials=False)
        self.assertFalse(result["credentials_enabled"])
        self.assertNotIn("local_credentials", parse((self.home / "config.toml").read_bytes())["mcp_servers"])
        self.assertNotIn("Saved logins", (self.home / "AGENTS.md").read_text())
        edit("install", self.app, self.home, self.state, self.db, credentials=True)
        self.invoke("uninstall")
        self.assertEqual((self.home / "config.toml").read_bytes(), self.config)
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), self.instructions)

    def test_unmanaged_credentials_preserved_until_explicit_opt_in(self):
        f = self.home / "config.toml"
        original = self.config + b'[mcp_servers.local_credentials]\ncommand = "other"\n'
        f.write_bytes(original)
        self.invoke("install")
        self.assertEqual(parse(f.read_bytes())["mcp_servers"]["local_credentials"], {"command": "other"})
        with self.assertRaises(ValueError):
            edit("install", self.app, self.home, self.state, self.db, credentials=True)
        self.invoke("uninstall")
        self.assertEqual(f.read_bytes(), original)
