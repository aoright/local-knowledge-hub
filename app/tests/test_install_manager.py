import importlib.util
import json
import os
import tempfile
import tomllib
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

    def test_antigravity_rule_uses_official_global_gemini_file(self):
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            legacy = home / ".gemini" / "config" / "AGENTS.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                manager.replace_managed_block("# User legacy\n", manager.INSTRUCTIONS),
                encoding="utf-8",
            )
            manager.configure_agents(home, True)
            gemini = home / ".gemini" / "GEMINI.md"
            self.assertIn(manager.MANAGED_BEGIN, gemini.read_text(encoding="utf-8"))
            legacy_content = legacy.read_text(encoding="utf-8")
            self.assertIn("# User legacy", legacy_content)
            self.assertNotIn(manager.MANAGED_BEGIN, legacy_content)

    def test_pre_managed_rule_is_migrated_without_duplication(self):
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            legacy = home / ".gemini" / "config" / "AGENTS.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                "# Global Rules\n\n## Keep Me\nValue\n\n"
                "## Shared Local Knowledge Automation\n\n- Old rule\n",
                encoding="utf-8",
            )
            codex = home / ".codex" / "AGENTS.md"
            codex.parent.mkdir(parents=True)
            codex.write_text(
                "# Shared local knowledge automation\n\n- Old rule\n",
                encoding="utf-8",
            )
            manager.configure_agents(home, True)
            self.assertEqual(
                codex.read_text(encoding="utf-8").count(manager.MANAGED_BEGIN), 1
            )
            legacy_content = legacy.read_text(encoding="utf-8")
            self.assertIn("## Keep Me", legacy_content)
            self.assertNotIn("Shared Local Knowledge", legacy_content)

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
            arguments = [str(home / "app" / "server.py"), "mcp"]
            environment = {"KHUB_DATA_DIR": str(home / "data")}
            self.assertTrue(
                manager.configure_codex(
                    home, command, arguments, environment, True
                )
            )
            content = config.read_text(encoding="utf-8")
            self.assertIn('[mcp_servers.other]', content)
            self.assertEqual(content.count('[mcp_servers.local-knowledge]'), 1)
            parsed = tomllib.loads(content)
            local = parsed["mcp_servers"]["local-knowledge"]
            self.assertEqual(local["command"], str(command))
            self.assertEqual(local["args"], arguments)
            self.assertEqual(local["env"], environment)
            self.assertTrue(
                manager.configure_codex(
                    home, command, arguments, environment, False
                )
            )
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
            arguments = [str(Path(value) / "server.py"), "mcp"]
            environment = {"KHUB_DATA_DIR": str(Path(value) / "data")}
            manager.configure_json_mcp(
                path, command, arguments, environment, True
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("other", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["local-knowledge"]["command"], str(command))
            self.assertEqual(data["mcpServers"]["local-knowledge"]["args"], arguments)
            self.assertEqual(data["mcpServers"]["local-knowledge"]["env"], environment)
            manager.configure_json_mcp(
                path, command, arguments, environment, False
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("other", data["mcpServers"])
            self.assertNotIn("local-knowledge", data["mcpServers"])

    def test_initialize_generates_private_unique_configuration(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / "home"
            install = root / "install"
            (install / "app" / "src").mkdir(parents=True)
            (install / "app" / "src" / "knowledge_hub.py").touch()
            (install / "venv" / "bin").mkdir(parents=True)
            (install / "venv" / "bin" / "python3").touch()
            with (
                mock.patch.object(manager.Path, "home", return_value=home),
                mock.patch.dict(
                    os.environ,
                    {"KHUB_SKIP_LAUNCHCTL": "1", "KHUB_PLATFORM": "macos"},
                ),
            ):
                result = manager.initialize(install, MODULE_PATH.parents[1], False)
            onyx = install / "data" / "config" / "onyx.env"
            searx = install / "data" / "config" / "searxng" / "settings.yml"
            self.assertTrue(onyx.is_file())
            self.assertTrue(searx.is_file())
            if os.name != "nt":
                self.assertEqual(onyx.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("{{", onyx.read_text(encoding="utf-8"))
            self.assertNotIn("{{", searx.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertTrue((install / "bin" / "khub").stat().st_mode & 0o100)
            self.assertIn("clients", result)

    def test_windows_initialize_writes_native_wrappers_and_mcp_launch(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            home = root / "home"
            install = root / "install"
            (install / "app" / "src").mkdir(parents=True)
            (install / "app" / "src" / "knowledge_hub.py").touch()
            (install / "venv" / "Scripts").mkdir(parents=True)
            (install / "venv" / "Scripts" / "python.exe").touch()
            with (
                mock.patch.object(manager.Path, "home", return_value=home),
                mock.patch.dict(
                    os.environ,
                    {"KHUB_SKIP_SCHEDULER": "1", "KHUB_PLATFORM": "windows"},
                ),
            ):
                result = manager.initialize(install, MODULE_PATH.parents[1], False)
                checks = manager.doctor(install)
            self.assertEqual(result["platform"], "windows")
            self.assertTrue((install / "bin" / "khub.cmd").is_file())
            self.assertTrue(checks["passed"])
            codex = tomllib.loads(
                (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            )["mcp_servers"]["local-knowledge"]
            self.assertEqual(
                codex["command"],
                str(install.resolve() / "venv" / "Scripts" / "python.exe"),
            )
            self.assertEqual(codex["args"][-1], "mcp")
            self.assertEqual(
                codex["env"]["KHUB_DATA_DIR"], str(install.resolve() / "data")
            )
            wrapper = (install / "bin" / "khub.cmd").read_text(encoding="utf-8")
            self.assertIn("%*", wrapper)
            self.assertIn("KHUB_DATA_DIR", wrapper)
            backup_wrapper = (
                install / "bin" / "knowledge-hub-backup.cmd"
            ).read_text(encoding="utf-8")
            self.assertIn('"--mode" "critical"', backup_wrapper)

    def test_windows_scheduled_tasks_use_per_user_limited_jobs(self):
        with tempfile.TemporaryDirectory() as value:
            install = Path(value)
            with mock.patch.object(manager, "schtasks") as scheduler:
                tasks = manager.install_scheduled_tasks(install, True)
            self.assertEqual(set(tasks), set(manager.WINDOWS_TASKS.values()))
            create_calls = [
                call.args[0] for call in scheduler.call_args_list if call.args[0][0] == "/Create"
            ]
            self.assertEqual(len(create_calls), 3)
            self.assertTrue(all("LIMITED" in arguments for arguments in create_calls))
            self.assertTrue(
                any("knowledge-hub-services.cmd" in " ".join(arguments) for arguments in create_calls)
            )


if __name__ == "__main__":
    unittest.main()
