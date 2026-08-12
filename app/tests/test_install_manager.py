import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "src" / "install_manager.py"
SPEC = importlib.util.spec_from_file_location("install_manager", MODULE_PATH)
assert SPEC and SPEC.loader
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


class InstallManagerTests(unittest.TestCase):
    def test_managed_agents_block_preserves_user_content_and_is_idempotent(self):
        original = "# User rules\n\nKeep this.\n"
        first = manager.replace_managed_block(original, manager.INSTRUCTIONS)
        second = manager.replace_managed_block(first, manager.INSTRUCTIONS)
        self.assertEqual(first, second)
        self.assertIn("Keep this.", first)
        self.assertEqual(first.count(manager.MANAGED_BEGIN), 1)
        removed = manager.replace_managed_block(first, None)
        self.assertIn("Keep this.", removed)
        self.assertNotIn(manager.MANAGED_BEGIN, removed)

    def test_codex_configuration_replaces_only_local_knowledge_sections(self):
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                '[mcp_servers.other]\ncommand = "other"\n\n'
                '[mcp_servers.local-knowledge]\ncommand = "/old/khub"\n\n'
                '[mcp_servers.local-knowledge.tools.web_search]\napproval_mode = "approve"\n',
                encoding="utf-8",
            )
            command = home / "install" / "bin" / "khub"
            self.assertTrue(manager.configure_codex(home, command, True))
            content = config.read_text(encoding="utf-8")
            self.assertIn('[mcp_servers.other]', content)
            self.assertEqual(content.count('[mcp_servers.local-knowledge]'), 1)
            self.assertIn(str(command), content)
            self.assertTrue(manager.configure_codex(home, command, False))
            content = config.read_text(encoding="utf-8")
            self.assertIn('[mcp_servers.other]', content)
            self.assertNotIn('mcp_servers.local-knowledge', content)

    def test_json_configuration_preserves_other_servers(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "mcp.json"
            path.write_text(
                json.dumps({"mcpServers": {"other": {"command": "other"}}}),
                encoding="utf-8",
            )
            command = Path(value) / "bin" / "khub"
            manager.configure_json_mcp(path, command, True)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("other", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["local-knowledge"]["command"], str(command))
            manager.configure_json_mcp(path, command, False)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("other", data["mcpServers"])
            self.assertNotIn("local-knowledge", data["mcpServers"])

    def test_initialize_generates_private_unique_configuration(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / "home"
            install = root / "install"
            (install / "app").mkdir(parents=True)
            (install / "venv" / "bin").mkdir(parents=True)
            (install / "venv" / "bin" / "python3").touch()
            with (
                mock.patch.object(manager.Path, "home", return_value=home),
                mock.patch.dict(os.environ, {"KHUB_SKIP_LAUNCHCTL": "1"}),
            ):
                result = manager.initialize(install, MODULE_PATH.parents[1], False)
            onyx = install / "data" / "config" / "onyx.env"
            searx = install / "data" / "config" / "searxng" / "settings.yml"
            self.assertTrue(onyx.is_file())
            self.assertTrue(searx.is_file())
            self.assertEqual(onyx.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("{{", onyx.read_text(encoding="utf-8"))
            self.assertNotIn("{{", searx.read_text(encoding="utf-8"))
            self.assertTrue((install / "bin" / "khub").stat().st_mode & 0o100)
            self.assertIn("clients", result)


if __name__ == "__main__":
    unittest.main()
