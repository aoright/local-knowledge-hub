import importlib.util
import json
import subprocess
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
discover = load("discover_projects")
maintenance = load("maintenance")


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
                mock.patch.object(start, "docker_running") as docker_running,
                mock.patch.object(start, "run") as runner,
            ):
                result = start.ensure_services()
        self.assertEqual(result["action"], "healthy")
        docker_running.assert_not_called()
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
                mock.patch.object(start.platform, "system", return_value="Darwin"),
                mock.patch.object(start, "docker_running", side_effect=[False, True]),
                mock.patch.object(start, "colima_running", return_value=False),
                mock.patch.object(start, "wait_for_services", return_value={"onyx": True, "searxng": True}),
                mock.patch.object(start, "run") as runner,
            ):
                result = start.ensure_services()
        self.assertEqual(result["action"], "started")
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(runner.call_args_list[0].args[0][0], str(colima))

    def test_service_watchdog_restarts_stale_colima_daemon(self):
        with tempfile.TemporaryDirectory() as value:
            colima = Path(value) / "colima"
            docker = Path(value) / "docker"
            colima.touch()
            docker.touch()
            with (
                mock.patch.object(start, "COLIMA", colima),
                mock.patch.object(start, "DOCKER", docker),
                mock.patch.object(start.platform, "system", return_value="Darwin"),
                mock.patch.object(start, "docker_running", return_value=False),
                mock.patch.object(start, "colima_running", return_value=True),
                mock.patch.object(start, "wait_for_docker", side_effect=[False, True]),
                mock.patch.object(start, "run") as runner,
            ):
                action = start.ensure_docker_runtime()
        self.assertEqual(action, "restarted")
        runner.assert_called_once_with([str(colima), "restart"], timeout=600)

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

    def test_windows_starts_docker_desktop_before_compose(self):
        with tempfile.TemporaryDirectory() as value:
            docker = Path(value) / "docker.exe"
            desktop = Path(value) / "Docker Desktop.exe"
            docker.touch()
            desktop.touch()
            with (
                mock.patch.object(start, "DOCKER", docker),
                mock.patch.object(start.platform, "system", return_value="Windows"),
                mock.patch.object(start, "healthy", side_effect=[False, False]),
                mock.patch.object(
                    start, "docker_running", side_effect=[False, False, True]
                ),
                mock.patch.object(start, "docker_desktop", return_value=desktop),
                mock.patch.object(start, "wait_for_docker", return_value=True),
                mock.patch.object(
                    start,
                    "wait_for_services",
                    return_value={"onyx": True, "searxng": True},
                ),
                mock.patch.object(start.subprocess, "Popen") as popen,
                mock.patch.object(start, "run") as runner,
            ):
                result = start.ensure_services()
            self.assertEqual(result["action"], "started")
            popen.assert_called_once()
            self.assertEqual(runner.call_count, 2)

    def test_stop_services_can_purge_volumes(self):
        with tempfile.TemporaryDirectory() as value:
            docker = Path(value) / "docker"
            docker.touch()
            with (
                mock.patch.object(start, "DOCKER", docker),
                mock.patch.object(start, "docker_running", return_value=True),
                mock.patch.object(start, "run") as runner,
            ):
                result = start.stop_services(True)
            self.assertEqual(result, {"action": "stopped", "purged": True})
            self.assertEqual(runner.call_count, 2)
            self.assertTrue(
                all("--volumes" in call.args[0] for call in runner.call_args_list)
            )

    def test_windows_file_uri_removes_drive_prefix_slash(self):
        self.assertEqual(
            discover.file_uri_path_text("file:///C:/Users/example/project", windows=True),
            "C:/Users/example/project",
        )
        self.assertEqual(
            discover.file_uri_path_text(
                "file://server/share/team/project", windows=True
            ),
            "//server/share/team/project",
        )

    def test_maintenance_lock_is_cross_platform(self):
        with tempfile.TemporaryDirectory() as value:
            lock_file = Path(value) / "maintenance.lock"
            with mock.patch.object(maintenance, "LOCK_FILE", lock_file):
                handle = maintenance.lock()
                self.assertTrue(lock_file.is_file())
                handle.close()

    def test_unchanged_git_project_skips_repeated_full_scan(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            project = root / "project"
            project.mkdir()
            db_path = root / "knowledge.sqlite3"
            state_path = root / "index-state.json"
            original_connect = maintenance.kh.connect

            db = original_connect(db_path)
            maintenance.kh.initialize(db)
            maintenance.kh.add_project(db, "alpha", "Alpha", str(project))
            db.close()

            with (
                mock.patch.object(
                    maintenance.kh,
                    "connect",
                    side_effect=lambda: original_connect(db_path),
                ),
                mock.patch.object(maintenance, "INDEX_STATE_FILE", state_path),
                mock.patch.object(
                    maintenance, "git_project_fingerprint", return_value="stable"
                ),
                mock.patch.object(
                    maintenance.kh,
                    "ingest_project",
                    return_value=maintenance.kh.IngestStats(project="alpha"),
                ) as ingest,
                mock.patch.object(
                    maintenance.kh, "maintain_memories", return_value={}
                ),
                mock.patch.object(
                    maintenance.kh, "backfill_memory_embeddings", return_value={}
                ),
            ):
                first = maintenance.ingest_all()
                second = maintenance.ingest_all()

            self.assertEqual(first["projects"], 1)
            self.assertEqual(second["projects"], 0)
            self.assertEqual(second["fingerprint_skipped"], 1)
            self.assertEqual(ingest.call_count, 1)

    def test_git_fingerprint_detects_untracked_file_edits(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            tracked = root / "tracked.txt"
            tracked.write_text("tracked", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(root), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-m", "initial",
                ],
                check=True,
                capture_output=True,
            )
            untracked = root / "draft.txt"
            untracked.write_text("first", encoding="utf-8")
            first = maintenance.git_project_fingerprint({"paths": [str(root)]})
            untracked.write_text("second version", encoding="utf-8")
            second = maintenance.git_project_fingerprint({"paths": [str(root)]})
            self.assertNotEqual(first, second)

    def test_git_collection_fingerprint_tracks_nested_repositories(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            repository = root / "nested"
            repository.mkdir()
            subprocess.run(
                ["git", "init", str(repository)], check=True, capture_output=True
            )
            tracked = repository / "tracked.txt"
            tracked.write_text("first", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", "tracked.txt"], check=True
            )
            subprocess.run(
                [
                    "git", "-C", str(repository), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-m", "initial",
                ],
                check=True,
                capture_output=True,
            )
            first = maintenance.git_project_fingerprint({"paths": [str(root)]})
            tracked.write_text("second", encoding="utf-8")
            second = maintenance.git_project_fingerprint({"paths": [str(root)]})
            self.assertIsNotNone(first)
            self.assertNotEqual(first, second)

    def test_critical_backup_preserves_memory_but_drops_rebuildable_index(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            project = root / "project"
            project.mkdir()
            (project / "code.md").write_text("rebuildable code", encoding="utf-8")
            db_path = root / "knowledge.sqlite3"
            backup_dir = root / "backups"
            db = maintenance.kh.connect(db_path)
            maintenance.kh.initialize(db)
            maintenance.kh.add_project(db, "alpha", "Alpha", str(project))
            maintenance.kh.ingest_project(db, "alpha")
            maintenance.kh.remember(
                db, "alpha", "Durable", "Keep this memory", "constraint"
            )

            with (
                mock.patch.object(maintenance, "BACKUP_DIR", backup_dir),
                mock.patch.object(maintenance.kh, "connect", return_value=db),
            ):
                result = maintenance.backup(retain=2, mode="critical")
                verified = maintenance.verify_backup(Path(result["backup"]))

            self.assertEqual(result["mode"], "critical")
            self.assertEqual(result["copied"]["documents"], 1)
            self.assertEqual(verified["mode"], "critical")
            self.assertEqual(verified["memories"], 1)
            self.assertEqual(verified["documents"], 1)
            self.assertFalse(any(backup_dir.glob("tmp*")))

    def test_checkpoint_database_reports_safe_wal_result(self):
        with tempfile.TemporaryDirectory() as value:
            db_path = Path(value) / "knowledge.sqlite3"
            db = maintenance.kh.connect(db_path)
            maintenance.kh.initialize(db)
            db.execute(
                "INSERT INTO audit_log(event_at,action,project_id,details_json) "
                "VALUES('2026-01-01T00:00:00+00:00','test',NULL,'{}')"
            )
            db.commit()
            db.close()
            result = maintenance.checkpoint_database(db_path, truncate=True)

        self.assertEqual(result["mode"], "truncate")
        self.assertEqual(result["busy"], 0)
        self.assertLessEqual(result["wal_bytes_after"], result["wal_bytes_before"])

    def test_backup_pruning_is_dry_run_until_explicitly_applied(self):
        with tempfile.TemporaryDirectory() as value:
            backup_dir = Path(value)
            for index in range(5):
                full = backup_dir / f"knowledge-hub-2026010{index}T000000Z.sqlite3.gz"
                full.write_bytes(b"full" * (index + 1))
                full.with_suffix(full.suffix + ".sha256").write_text("checksum\n")
            for index in range(4):
                critical = backup_dir / f"knowledge-hub-critical-2026010{index}T000000Z.sqlite3.gz"
                critical.write_bytes(b"critical" * (index + 1))

            with mock.patch.object(maintenance, "BACKUP_DIR", backup_dir):
                preview = maintenance.prune_backups(2, 2, apply=False)
                self.assertEqual(len(list(backup_dir.glob("*.sqlite3.gz"))), 9)
                applied = maintenance.prune_backups(2, 2, apply=True)

            self.assertFalse(preview["applied"])
            self.assertEqual(preview["remove_count"], 5)
            self.assertGreater(preview["reclaimable_bytes"], 0)
            self.assertTrue(applied["applied"])
            self.assertEqual(len(list(backup_dir.glob("*.sqlite3.gz"))), 4)

    def test_empty_project_review_only_prunes_explicit_stale_missing_paths(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            existing = root / "existing"
            missing = root / "missing"
            existing.mkdir()
            missing.mkdir()
            db_path = root / "knowledge.sqlite3"
            db = maintenance.kh.connect(db_path)
            maintenance.kh.initialize(db)
            maintenance.kh.add_project(db, "existing", "Existing", str(existing))
            maintenance.kh.add_project(db, "missing", "Missing", str(missing))
            db.execute(
                "UPDATE projects SET updated_at='2020-01-01T00:00:00+00:00' "
                "WHERE slug IN ('existing','missing')"
            )
            db.commit()
            db.close()
            missing.rmdir()

            preview = maintenance.review_empty_projects(
                minimum_age_days=7, db_path=db_path
            )
            with self.assertRaises(ValueError):
                maintenance.review_empty_projects(
                    apply=True, minimum_age_days=7, db_path=db_path
                )
            with self.assertRaises(ValueError):
                maintenance.review_empty_projects(
                    apply=True,
                    project_slugs=["existing"],
                    minimum_age_days=7,
                    db_path=db_path,
                )
            applied = maintenance.review_empty_projects(
                apply=True,
                project_slugs=["missing"],
                minimum_age_days=7,
                db_path=db_path,
            )
            existing_preview = maintenance.review_empty_projects(
                minimum_age_days=7,
                allow_existing_paths=True,
                db_path=db_path,
            )
            existing_applied = maintenance.review_empty_projects(
                apply=True,
                project_slugs=["existing"],
                minimum_age_days=7,
                allow_existing_paths=True,
                db_path=db_path,
            )
            check = maintenance.kh.connect(db_path)
            remaining = [
                row[0] for row in check.execute(
                    "SELECT slug FROM projects WHERE scope_type='project' ORDER BY slug"
                )
            ]
            audit_count = check.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='project.metadata_pruned'"
            ).fetchone()[0]
            check.close()
            existing_still_exists = existing.is_dir()

        self.assertEqual(preview["zero_document_count"], 2)
        self.assertEqual([item["slug"] for item in preview["candidates"]], ["missing"])
        self.assertEqual(applied["removed"], ["missing"])
        self.assertEqual(
            [item["slug"] for item in existing_preview["candidates"]],
            ["existing"],
        )
        self.assertEqual(existing_applied["removed"], ["existing"])
        self.assertTrue(existing_still_exists)
        self.assertEqual(remaining, [])
        self.assertEqual(audit_count, 2)

    def test_memory_quality_review_quarantines_without_deleting(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            project = root / "project"
            project.mkdir()
            db_path = root / "knowledge.sqlite3"
            db = maintenance.kh.connect(db_path)
            maintenance.kh.initialize(db)
            maintenance.kh.add_project(db, "alpha", "Alpha", str(project))
            good = maintenance.kh.remember(
                db, "alpha", "Durable rule", "Production releases require regression tests",
                "constraint", {"capture_mode": "automatic", "evidence": "以后生产发布必须先通过回归测试"},
            )
            noisy = maintenance.kh.remember(
                db, "alpha", "Button feedback", "Move the button to the left",
                "decision", {"capture_mode": "automatic", "evidence": "这个按钮怎么还在这里？"},
            )
            db.close()

            preview = maintenance.review_automatic_memories(False, db_path)
            applied = maintenance.review_automatic_memories(True, db_path)
            check = maintenance.kh.connect(db_path)
            good_status = maintenance.kh.get_memory(check, good["document_id"])["status"]
            noisy_memory = maintenance.kh.get_memory(check, noisy["document_id"])
            history_events = [
                row[0] for row in check.execute(
                    "SELECT event FROM memory_history WHERE document_id=? ORDER BY version_no",
                    (noisy["document_id"],),
                )
            ]
            check.close()

        self.assertFalse(preview["applied"])
        self.assertEqual(preview["review_count"], 1)
        self.assertTrue(applied["applied"])
        self.assertEqual(good_status, "active")
        self.assertEqual(noisy_memory["status"], "candidate")
        self.assertIn("quality_quarantined", history_events)


if __name__ == "__main__":
    unittest.main()
