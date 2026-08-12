import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SRC = Path(__file__).parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SRC / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


start = load("start_services")
catalog = load("export_mcp_catalog")


class OperationsTests(unittest.TestCase):
    def test_service_watchdog_is_idempotent_when_healthy(self):
        with tempfile.TemporaryDirectory() as value:
            colima = Path(value) / "colima"
            docker = Path(value) / "docker"
            colima.touch()
            docker.touch()
            with (
                mock.patch.object(start, "COLIMA", colima),
                mock.patch.object(start, "DOCKER", docker),
                mock.patch.object(start, "healthy", side_effect=[True, True]),
                mock.patch.object(start, "docker_running", return_value=True),
                mock.patch.object(start, "run") as runner,
            ):
                result = start.ensure_services()
        self.assertEqual(result["action"], "healthy")
        runner.assert_not_called()

    def test_service_watchdog_starts_vm_and_both_stacks(self):
        with tempfile.TemporaryDirectory() as value:
            colima = Path(value) / "colima"
            docker = Path(value) / "docker"
            colima.touch()
            docker.touch()
            with (
                mock.patch.object(start, "COLIMA", colima),
                mock.patch.object(start, "DOCKER", docker),
                mock.patch.object(start, "healthy", side_effect=[False, False]),
                mock.patch.object(start, "docker_running", side_effect=[False, True]),
                mock.patch.object(start, "colima_running", return_value=False),
                mock.patch.object(start, "wait_for_services", return_value={"onyx": True, "searxng": True}),
                mock.patch.object(start, "run") as runner,
            ):
                result = start.ensure_services()
        self.assertEqual(result["action"], "started")
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(runner.call_args_list[0].args[0][0], str(colima))

    def test_exported_catalog_matches_gateway_tools(self):
        with tempfile.TemporaryDirectory() as value:
            target = Path(value) / "catalog"
            result = catalog.export(target)
            self.assertEqual(result["files"], len(catalog.kh.mcp_tools()) + 1)
            context = json.loads((target / "knowledge_context.json").read_text(encoding="utf-8"))
            self.assertEqual(context["name"], "knowledge_context")
            self.assertEqual(
                (target / "instructions.md").read_text(encoding="utf-8"),
                catalog.kh.MCP_INSTRUCTIONS,
            )


if __name__ == "__main__":
    unittest.main()
