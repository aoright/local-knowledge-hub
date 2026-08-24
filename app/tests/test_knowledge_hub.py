import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "src" / "knowledge_hub.py"
SPEC = importlib.util.spec_from_file_location("knowledge_hub", MODULE_PATH)
assert SPEC and SPEC.loader
kh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = kh
SPEC.loader.exec_module(kh)


class KnowledgeHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.db = kh.connect(Path(self.tmp.name) / "test.sqlite3")
        kh.initialize(self.db)
        kh.add_project(self.db, "alpha", "Alpha", str(self.root))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_ingest_is_idempotent_and_project_scoped(self):
        (self.root / "note.md").write_text("unique-alpha-marker architecture", encoding="utf-8")
        first = kh.ingest_project(self.db, "alpha")
        second = kh.ingest_project(self.db, "alpha")
        self.assertEqual(first.indexed, 1)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(len(kh.search(self.db, "alpha", "unique-alpha-marker")), 1)

    def test_index_policy_keeps_manifests_and_recent_files_within_budget(self):
        policy = Path(self.tmp.name) / "index-policy.json"
        policy.write_text(json.dumps({
            "defaults": {
                "max_documents": 2,
                "max_chunks": 20,
                "max_chunks_per_document": 10,
            }
        }), encoding="utf-8")
        readme = self.root / "README.md"
        recent = self.root / "recent.md"
        old = self.root / "old.md"
        readme.write_text("manifest-marker", encoding="utf-8")
        recent.write_text("recent-marker", encoding="utf-8")
        old.write_text("old-marker", encoding="utf-8")
        os.utime(old, (1, 1))
        os.utime(recent, (2, 2))
        os.utime(readme, (1, 1))
        with mock.patch.object(kh, "INDEX_POLICY_FILE", policy):
            stats = kh.ingest_project(self.db, "alpha")
        self.assertTrue(stats.policy_limited)
        self.assertEqual(stats.selected_documents, 2)
        self.assertEqual(len(kh.search(self.db, "alpha", "manifest-marker")), 1)
        self.assertEqual(len(kh.search(self.db, "alpha", "recent-marker")), 1)
        self.assertEqual(kh.search(self.db, "alpha", "old-marker"), [])

    def test_index_policy_caps_chunks_per_document(self):
        policy = Path(self.tmp.name) / "index-policy.json"
        policy.write_text(json.dumps({
            "defaults": {
                "max_documents": 10,
                "max_chunks": 10,
                "max_chunks_per_document": 2,
            }
        }), encoding="utf-8")
        (self.root / "large.md").write_text("chunk-marker\n" * 1000, encoding="utf-8")
        with mock.patch.object(kh, "INDEX_POLICY_FILE", policy):
            stats = kh.ingest_project(self.db, "alpha")
        self.assertEqual(stats.truncated_documents, 1)
        self.assertEqual(stats.selected_chunks, 2)

    def test_existing_over_budget_index_is_preserved_until_explicit_review(self):
        for index in range(3):
            (self.root / f"file-{index}.md").write_text(
                f"preserve-marker-{index}", encoding="utf-8"
            )
        kh.ingest_project(self.db, "alpha")
        policy = Path(self.tmp.name) / "index-policy.json"
        policy.write_text(json.dumps({
            "defaults": {
                "max_documents": 2,
                "max_chunks": 20,
                "max_chunks_per_document": 10,
            }
        }), encoding="utf-8")
        with mock.patch.object(kh, "INDEX_POLICY_FILE", policy):
            stats = kh.ingest_project(self.db, "alpha")
        self.assertEqual(stats.policy_reason, "existing_index_exceeds_policy")
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM documents WHERE source_type='file'"
            ).fetchone()[0],
            3,
        )

    def test_search_succeeds_when_telemetry_writer_is_busy(self):
        (self.root / "note.md").write_text("nonblocking-audit-marker", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        database = self.db.execute("PRAGMA database_list").fetchone()[2]
        blocker = sqlite3.connect(database)
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            results = kh.search(self.db, "alpha", "nonblocking-audit-marker")
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(len(results), 1)

    def test_secret_file_is_excluded_and_inline_secret_redacted(self):
        (self.root / ".env").write_text("PASSWORD=should-never-index", encoding="utf-8")
        (self.root / "config.md").write_text("api_key=abcdefghijklmnop visible-text", encoding="utf-8")
        stats = kh.ingest_project(self.db, "alpha")
        self.assertGreaterEqual(stats.secret_redactions, 1)
        self.assertEqual(kh.search(self.db, "alpha", "should-never-index"), [])
        result = kh.search(self.db, "alpha", "visible-text")
        self.assertIn("[REDACTED_SECRET]", result[0]["content"])

    def test_generated_test_logs_are_excluded_and_existing_noise_is_removed(self):
        logs = self.root / "logs"
        logs.mkdir()
        generated = logs / "test.log"
        generated.write_text("generated-test-log-marker", encoding="utf-8")
        (logs / "runtime.log").write_text("useful-runtime-log-marker", encoding="utf-8")

        source_key = f"{self.root.name}/logs/test.log"
        document_id = kh.stable_document_id(
            kh.get_project(self.db, "alpha")["id"], "file", source_key
        )
        now = kh.utcnow()
        self.db.execute(
            "INSERT INTO documents(id,project_id,source_type,source_key,title,relative_path,"
            "source_uri,content_hash,byte_size,modified_at,indexed_at,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                kh.get_project(self.db, "alpha")["id"],
                "file",
                source_key,
                "logs/test.log",
                "logs/test.log",
                generated.as_uri(),
                kh.sha256_bytes(b"generated-test-log-marker"),
                generated.stat().st_size,
                now,
                now,
                "{}",
            ),
        )
        self.db.execute(
            "INSERT INTO chunks(document_id,project_id,chunk_index,title,relative_path,content) "
            "VALUES(?,?,?,?,?,?)",
            (
                document_id,
                kh.get_project(self.db, "alpha")["id"],
                0,
                "logs/test.log",
                "logs/test.log",
                "generated-test-log-marker",
            ),
        )
        self.db.commit()

        stats = kh.ingest_project(self.db, "alpha")

        self.assertEqual(stats.deleted, 1)
        self.assertEqual(kh.search(self.db, "alpha", "generated-test-log-marker"), [])
        self.assertEqual(len(kh.search(self.db, "alpha", "useful-runtime-log-marker")), 1)

    def test_secret_redaction_covers_chinese_credentials_and_commands(self):
        samples = [
            "服务器密码是 short-pass9，请不要泄露",
            "口令: abc12345 后继续操作",
            "Authorization: Bearer very.secret.token.value",
            "sshpass -p 'danger-pass' ssh user@example.com",
            "https://admin:danger-pass@example.com/private",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                redacted, count = kh.redact_secrets(sample)
                self.assertGreaterEqual(count, 1)
                self.assertIn("[REDACTED_SECRET]", redacted)
                self.assertNotIn("danger-pass", redacted)

    def test_controlled_memory(self):
        first = kh.remember(self.db, "alpha", "Decision", "Use stable project IDs", "decision")
        second = kh.remember(self.db, "alpha", "Duplicate", "Use stable project IDs", "decision")
        self.assertFalse(first["already_existed"])
        self.assertTrue(second["already_existed"])
        self.assertEqual(len(kh.search(self.db, "alpha", "stable project IDs")), 1)

    def test_status_aggregates_project_counts(self):
        (self.root / "one.md").write_text("first status marker", encoding="utf-8")
        (self.root / "two.md").write_text("second status marker", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        project = kh.status(self.db)["projects"][0]
        self.assertEqual(project["documents"], 2)
        self.assertEqual(project["chunks"], 2)
        self.assertGreater(project["bytes"], 0)
        self.assertFalse(project["index_over_budget"])
        self.assertEqual(project["index_policy"]["max_documents"], 25_000)

    def test_status_index_budget_excludes_long_term_memories(self):
        (self.root / "one.md").write_text("first status marker", encoding="utf-8")
        (self.root / "two.md").write_text("second status marker", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        kh.remember(self.db, "alpha", "Decision", "Keep durable project memory", "decision")
        policy = {
            "max_documents": 2,
            "max_chunks": 2,
            "max_chunks_per_document": 4_096,
            "exclude_globs": [],
        }
        with mock.patch.object(kh, "load_index_policy", return_value=policy):
            project = kh.status(self.db)["projects"][0]
        self.assertEqual(project["documents"], 3)
        self.assertEqual(project["chunks"], 3)
        self.assertFalse(project["index_over_budget"])

    def test_mcp_tool_catalog_is_json_serializable(self):
        tools = kh.mcp_tools()
        encoded = json.dumps(tools)
        self.assertIn('"knowledge_search"', encoded)
        remember_tool = next(tool for tool in tools if tool["name"] == "knowledge_remember")
        self.assertIs(remember_tool["inputSchema"]["properties"]["confirmed"]["const"], True)
        capture_tool = next(tool for tool in tools if tool["name"] == "knowledge_capture")
        self.assertNotIn("confidence", capture_tool["inputSchema"]["required"])
        self.assertNotIn("workspace_path", capture_tool["inputSchema"]["required"])
        self.assertIn("validation_rejected", capture_tool["description"])
        context_tool = next(tool for tool in tools if tool["name"] == "knowledge_context")
        self.assertEqual(
            context_tool["inputSchema"]["required"], ["query", "workspace_path"]
        )
        self.assertEqual(
            context_tool["inputSchema"]["properties"]["workspace_path"]["minLength"],
            1,
        )
        self.assertIn("knowledge_context", [tool["name"] for tool in tools])
        self.assertIn("knowledge_capture", [tool["name"] for tool in tools])
        self.assertEqual(len(tools), 13)
        self.assertIn("knowledge_update", [tool["name"] for tool in tools])
        self.assertIn("knowledge_forget", [tool["name"] for tool in tools])
        self.assertIn("knowledge_move", [tool["name"] for tool in tools])
        update_tool = next(tool for tool in tools if tool["name"] == "knowledge_update")
        self.assertIn("一次性提交全部必填参数", update_tool["description"])
        self.assertIn("不要猜测", update_tool["description"])
        self.assertEqual(
            update_tool["inputSchema"]["properties"]["memory_id"]["minLength"], 1
        )

    def test_mcp_stdio_is_utf8_and_ignores_blank_frames(self):
        db_path = Path(self.tmp.name) / "mcp.sqlite3"
        requests = "\n" + "\n".join(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {},
                    }
                ),
                json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "knowledge_status", "arguments": {}},
                    }
                ),
            )
        ) + "\n"
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--db", str(db_path), "mcp"],
            input=requests,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertIn(
            "knowledge_context",
            [tool["name"] for tool in responses[1]["result"]["tools"]],
        )
        self.assertNotIn('"error"', result.stdout)
        audit_db = sqlite3.connect(db_path)
        audit_rows = audit_db.execute(
            "SELECT action,details_json FROM audit_log ORDER BY id"
        ).fetchall()
        audit_db.close()
        self.assertEqual([row[0] for row in audit_rows], ["mcp.initialize", "mcp.tool_call"])
        self.assertEqual(json.loads(audit_rows[1][1])["tool"], "knowledge_status")

    def test_resolve_project_from_workspace_path(self):
        nested = self.root / "src" / "module"
        nested.mkdir(parents=True)
        self.assertEqual(
            kh.resolve_project_reference(self.db, workspace_path=str(nested)),
            "alpha",
        )
        self.assertEqual(
            kh.resolve_project_reference(self.db, workspace_path=self.root.name),
            "alpha",
        )
        with self.assertRaises(ValueError):
            kh.resolve_project_reference(self.db, project_ref="definitely-not-a-project")

    def test_selects_unique_recent_antigravity_workspace(self):
        recent = "2026-08-15T06:00:00Z"
        older = "2026-08-15T05:00:00Z"
        summaries = {
            "current": {
                "lastUserInputTime": recent,
                "workspaces": [{
                    "workspaceFolderAbsoluteUri": "file:///tmp/current-project"
                }],
            },
            "older": {
                "lastUserInputTime": older,
                "workspaces": [{
                    "workspaceFolderAbsoluteUri": "file:///tmp/older-project"
                }],
            },
        }
        now = kh.datetime.fromisoformat(recent.replace("Z", "+00:00")).timestamp() + 5
        self.assertEqual(
            kh.select_recent_antigravity_workspace(summaries, now=now),
            str(Path("/tmp/current-project").resolve(strict=False)),
        )

    def test_client_surface_uses_parent_process_without_persisting_arguments(self):
        ide_command = (
            "/Applications/Antigravity IDE.app/Contents/language_server "
            "--csrf_token do-not-store --app_data_dir antigravity-ide "
            "--subclient_type ide"
        )
        standalone_command = (
            "/Applications/Antigravity.app/Contents/language_server "
            "--csrf_token do-not-store --standalone"
        )
        self.assertEqual(
            kh.infer_client_surface("antigravity-client", ide_command),
            "antigravity-ide",
        )
        self.assertEqual(
            kh.infer_client_surface("antigravity-client", standalone_command),
            "antigravity",
        )
        self.assertEqual(kh.infer_client_surface("codex-mcp-client"), "codex")
        with mock.patch.object(
            kh, "parent_process_command", return_value=ide_command
        ):
            identity = kh.client_runtime_identity("antigravity-client")
        self.assertEqual(identity["surface"], "antigravity-ide")
        self.assertNotIn("command", identity)
        self.assertNotIn("do-not-store", json.dumps(identity))

    def test_rejects_ambiguous_recent_antigravity_workspaces(self):
        recent = "2026-08-15T06:00:00Z"
        summaries = {
            "one": {
                "lastUserInputTime": recent,
                "workspaces": [{"workspaceFolderAbsoluteUri": "file:///tmp/one"}],
            },
            "two": {
                "lastUserInputTime": recent,
                "workspaces": [{"workspaceFolderAbsoluteUri": "file:///tmp/two"}],
            },
        }
        now = kh.datetime.fromisoformat(recent.replace("Z", "+00:00")).timestamp()
        self.assertIsNone(kh.select_recent_antigravity_workspace(summaries, now=now))

    def test_antigravity_context_uses_active_workspace_when_arguments_omit_scope(self):
        with mock.patch.object(
            kh, "antigravity_active_workspace", return_value=str(self.root)
        ):
            value = kh.mcp_call(
                self.db,
                "knowledge_context",
                {"query": "automatic-scope-marker"},
                client_name="antigravity-client",
            )
        self.assertEqual(value["scope"]["slug"], "alpha")
        self.assertEqual(value["resolution_source"], "antigravity_active_workspace")

    def test_antigravity_context_auto_registers_new_git_workspace(self):
        workspace = Path(self.tmp.name) / "New Hardware Project"
        workspace.mkdir()
        (workspace / ".git").mkdir()
        (workspace / "requirements.md").write_text(
            "hardware-registration-bom-marker", encoding="utf-8"
        )
        with mock.patch.object(
            kh, "antigravity_active_workspace", return_value=str(workspace)
        ):
            value = kh.mcp_call(
                self.db,
                "knowledge_context",
                {"query": "hardware-registration-bom-marker"},
                client_name="antigravity-client",
            )
        self.assertEqual(value["scope"]["slug"], "new-hardware-project")
        self.assertEqual(value["result_counts"]["primary"], 1)
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='project.auto_registered'"
            ).fetchone()[0],
            1,
        )

    def test_explicit_workspace_auto_registers_non_git_document_project(self):
        workspace = Path(self.tmp.name) / "Architect Exam"
        workspace.mkdir()
        (workspace / "study-notes.md").write_text(
            "interactive-presentation-architect-marker", encoding="utf-8"
        )
        value = kh.mcp_call(
            self.db,
            "knowledge_context",
            {
                "workspace_path": str(workspace),
                "query": "interactive-presentation-architect-marker",
            },
            client_name="codex-mcp-client",
        )
        self.assertEqual(value["scope"]["slug"], "architect-exam")
        self.assertEqual(value["result_counts"]["primary"], 1)
        details = json.loads(
            self.db.execute(
                "SELECT details_json FROM audit_log "
                "WHERE action='project.auto_registered' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        self.assertEqual(details["source"], "arguments")
        self.assertFalse(details["require_git"])

    def test_inferred_non_git_workspace_still_fails_closed(self):
        workspace = Path(self.tmp.name) / "Untrusted Folder"
        workspace.mkdir()
        with mock.patch.object(
            kh, "antigravity_active_workspace", return_value=str(workspace)
        ):
            value = kh.mcp_call(
                self.db,
                "knowledge_context",
                {"query": "marker"},
                client_name="antigravity-client",
            )
        self.assertEqual(
            value["resolution_required"]["code"], "unresolved_project"
        )

    def test_pdf_indexes_title_when_extraction_is_unavailable(self):
        pdf = self.root / "系统架构师知识点.pdf"
        pdf.write_bytes(b"%PDF-1.4\nnot-a-real-pdf")
        with mock.patch.object(
            kh,
            "extract_pdf_text",
            return_value=(None, "pdf-title-only:no-extractable-text"),
        ):
            stats = kh.ingest_project(self.db, "alpha")
        self.assertEqual(stats.indexed, 1)
        result = kh.search(self.db, "alpha", "架构师知识点")
        self.assertEqual(len(result), 1)
        self.assertIn("文件名：系统架构师知识点", result[0]["content"])

    def test_pdf_extraction_timeout_falls_back_without_aborting(self):
        pdf = self.root / "timeout.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        with mock.patch.object(kh.shutil, "which", return_value="pdftotext"), mock.patch.object(
            kh.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("pdftotext", 90),
        ), mock.patch.dict(sys.modules, {"pypdf": None}):
            text, extraction = kh.extract_pdf_text(pdf)
        self.assertIsNone(text)
        self.assertEqual(extraction, "pdf-title-only:no-extractable-text")

    def test_resolve_project_accepts_unique_human_suffix_without_scope_leak(self):
        other_root = Path(self.tmp.name) / "nunu"
        other_root.mkdir()
        kh.add_project(self.db, "nunu", "nunu", str(other_root))
        server_root = other_root / "nunu-server-go-main"
        server_root.mkdir()
        kh.add_project(
            self.db, "nunu-server-go-main", "nunu-server-go-main", str(server_root)
        )

        self.assertEqual(
            kh.resolve_project_reference(
                self.db, project_ref="nunu-server-go-main / 智能体测试"
            ),
            "nunu-server-go-main",
        )
        self.assertEqual(
            kh.resolve_project_reference(
                self.db, project_ref="project: nunu-server-go-main（后端）"
            ),
            "nunu-server-go-main",
        )

    def test_unresolved_context_returns_safe_resolution_request(self):
        result = kh.mcp_call(
            self.db,
            "knowledge_context",
            {"project": "definitely-not-a-project", "query": "marker"},
        )
        self.assertEqual(result["result_counts"]["primary"], 0)
        self.assertEqual(
            result["resolution_required"]["code"], "unresolved_project"
        )
        self.assertEqual(result["results"], [])

    def test_mcp_failure_audit_records_safe_error_code_and_server_pid(self):
        db_path = Path(self.tmp.name) / "mcp-error.sqlite3"
        requests = "\n".join(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"clientInfo": {"name": "audit-test", "version": "1"}},
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "knowledge_search",
                            "arguments": {
                                "project": "definitely-not-a-project",
                                "query": "marker",
                            },
                        },
                    }
                ),
            )
        ) + "\n"
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--db", str(db_path), "mcp"],
            input=requests,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[1]["error"]["code"], -32000)
        audit_db = sqlite3.connect(db_path)
        details = json.loads(
            audit_db.execute(
                "SELECT details_json FROM audit_log WHERE action='mcp.tool_call'"
            ).fetchone()[0]
        )
        audit_db.close()
        self.assertEqual(details["error_code"], "unresolved_project")
        self.assertEqual(details["error_class"], "ProjectResolutionError")
        self.assertGreater(details["server_pid"], 0)

    def test_automatic_capture_is_strict_and_deduplicated(self):
        with self.assertRaises(ValueError):
            kh.capture_memory(
                self.db, "alpha", "Weak", "Use stable IDs", "decision", "Maybe use IDs", 0.8
            )
        first = kh.capture_memory(
            self.db,
            "alpha",
            "Stable identity",
            "Project identity must use stable UUIDs",
            "constraint",
            "以后项目身份必须使用稳定 UUID",
            0.95,
        )
        second = kh.capture_memory(
            self.db,
            "alpha",
            "Duplicate",
            "Project identity must use stable UUIDs",
            "constraint",
            "以后项目身份必须使用稳定 UUID",
            0.95,
        )
        self.assertFalse(first["already_existed"])
        self.assertTrue(second["already_existed"])
        self.assertEqual(first["capture_mode"], "automatic")

    def test_global_scopes_are_separate_from_real_projects(self):
        self.assertEqual([item["slug"] for item in kh.list_projects(self.db)], ["alpha"])
        self.assertEqual(
            {item["slug"] for item in kh.list_global_scopes(self.db)},
            set(kh.GLOBAL_SCOPES),
        )
        status = kh.status(self.db)
        self.assertEqual(len(status["projects"]), 1)
        self.assertEqual(len(status["global_scopes"]), 4)

    def test_auto_scope_routes_global_only_when_explicit(self):
        project_memory = kh.capture_memory_auto(
            self.db, "auto", "alpha", str(self.root), "Project rule",
            "This project uses project-scope-marker UUIDs", "constraint",
            "这个项目必须使用 project-scope-marker UUID", 0.95,
        )
        self.assertEqual(project_memory["resolved_scope"], "alpha")
        global_memory = kh.capture_memory_auto(
            self.db, "auto", "alpha", str(self.root), "Global rule",
            "All projects must use global-release-marker checks", "constraint",
            "所有项目必须运行 global-release-marker 检查", 0.99,
        )
        self.assertEqual(global_memory["resolved_scope"], "global-engineering")
        with self.assertRaises(ValueError):
            kh.capture_memory_auto(
                self.db, "global", "alpha", str(self.root), "Weak global",
                "All projects use weak-global-marker", "fact",
                "所有项目使用 weak-global-marker", 0.95,
            )

    def test_auto_capture_rejects_questions_and_transient_ui_feedback(self):
        for evidence in (
            "为什么今天的图标会少一条线并且变黑？",
            "这个按钮怎么还显示在这里",
            "太丑了，黄色很突兀，整体也没有线条美感",
        ):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                kh.capture_memory_auto(
                    self.db, "auto", "alpha", str(self.root), "UI feedback",
                    "Generated summary claims this is a durable global decision",
                    "decision", evidence, 0.99,
                )

    def test_generated_global_label_cannot_promote_project_memory(self):
        memory = kh.capture_memory_auto(
            self.db, "auto", "alpha", str(self.root), "全局烧录流水",
            "全局烧录流水默认直接修改数字并置顶", "decision",
            "这个项目以后默认允许烧录流水直接修改数字", 0.95,
        )
        self.assertEqual(memory["resolved_scope"], "alpha")

    def test_explicit_global_capture_requires_global_user_evidence(self):
        with self.assertRaises(ValueError):
            kh.capture_memory_auto(
                self.db, "global-engineering", "alpha", str(self.root),
                "Generated global title", "All projects use generated-global-marker",
                "constraint", "这个项目以后必须使用 generated-global-marker", 0.99,
            )

    def test_durable_project_naming_rule_survives_incidental_question(self):
        memory = kh.capture_memory_auto(
            self.db, "auto", "alpha", str(self.root), "Project identity",
            "The project name and visual identity are fixed", "decision",
            "项目名称叫 Alpha Control，包括服务目录名称也统一为 Alpha Control；"
            "界面使用纯白极简风格，为什么这里还有旧图标？",
            0.95,
        )
        self.assertEqual(memory["resolved_scope"], "alpha")

    def test_auto_capture_defaults_confidence_without_weakening_scope_rules(self):
        project_memory = kh.capture_memory_auto(
            self.db, "auto", "alpha", str(self.root), "Project default",
            "This project keeps project-default-marker", "fact",
            "这个项目长期使用 project-default-marker",
        )
        global_memory = kh.capture_memory_auto(
            self.db, "auto", "alpha", str(self.root), "Global default",
            "All projects keep global-default-marker", "constraint",
            "所有项目必须使用 global-default-marker",
        )
        self.assertEqual(project_memory["resolved_scope"], "alpha")
        self.assertEqual(project_memory["confidence"], 0.95)
        self.assertTrue(project_memory["confidence_defaulted"])
        self.assertEqual(global_memory["resolved_scope"], "global-engineering")
        self.assertEqual(global_memory["confidence"], 0.99)

    def test_context_always_returns_memory_completion_protocol(self):
        (self.root / "note.md").write_text("completion-protocol-marker", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        context = kh.context_search(self.db, "alpha", "completion-protocol-marker")
        self.assertTrue(context["completion_actions"]["memory_review_required"])
        self.assertIn("knowledge_capture", context["completion_actions"]["instruction"])

    def test_empty_scope_search_short_circuits_before_fts(self):
        with mock.patch.object(kh, "fts_query", side_effect=AssertionError("FTS should not run")):
            self.assertEqual(kh.search(self.db, "global-engineering", "empty-global-marker"), [])
        details = json.loads(
            self.db.execute(
                "SELECT details_json FROM audit_log WHERE action='knowledge.search' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        self.assertEqual(details["short_circuit"], "empty_scope")

    def test_global_aggregate_is_not_reported_as_user_collection(self):
        kh.remember(
            self.db,
            "global-engineering",
            "Global telemetry rule",
            "global-telemetry-marker",
            "constraint",
        )
        results = kh.search(
            self.db, f"collection:{kh.GLOBAL_COLLECTION_SLUG}", "global-telemetry-marker"
        )
        self.assertEqual(len(results), 1)
        details = json.loads(
            self.db.execute(
                "SELECT details_json FROM audit_log WHERE action='knowledge.search' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        self.assertEqual(details["scope_type"], "global")
        self.assertEqual(details["scope_slug"], kh.GLOBAL_COLLECTION_SLUG)

    def test_semantic_search_skips_embedding_when_scope_has_no_memories(self):
        project_id = kh.get_project(self.db, "alpha")["id"]
        with mock.patch.object(kh, "embed_query", side_effect=AssertionError("embedding should not run")):
            self.assertEqual(
                kh.semantic_search_memories(self.db, [project_id], "no-memory-marker"), []
            )

    def test_query_embedding_is_cached_across_scope_searches(self):
        kh.embed_query.cache_clear()
        sentinel = object()
        with mock.patch.object(kh, "embed_texts", return_value=[sentinel]) as embed:
            self.assertIs(kh.embed_query("same-query-marker"), sentinel)
            self.assertIs(kh.embed_query("same-query-marker"), sentinel)
        self.assertEqual(embed.call_count, 1)
        kh.embed_query.cache_clear()

    def test_context_never_blocks_on_cold_embedding_model(self):
        with mock.patch.object(
            kh,
            "upsert_memory_embedding",
            return_value={"embedded": False, "reason": "test"},
        ):
            memory = kh.remember(
                self.db,
                "alpha",
                "Cold semantic memory",
                "A differently worded durable architecture decision",
                "decision",
            )
        self.db.execute(
            "INSERT OR REPLACE INTO memory_embeddings(document_id,model,dimensions,embedding,content_hash,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                memory["document_id"],
                kh.EMBEDDING_MODEL,
                kh.EMBEDDING_DIMENSIONS,
                b"not-read-on-cold-path",
                "test-hash",
                kh.utcnow(),
            ),
        )
        self.db.commit()
        original_instance = kh._EMBEDDING_MODEL_INSTANCE
        original_ready = kh._EMBEDDING_MODEL_READY.is_set()
        kh._EMBEDDING_MODEL_INSTANCE = None
        kh._EMBEDDING_MODEL_READY.clear()
        try:
            with mock.patch.object(
                kh, "embed_query", side_effect=AssertionError("cold embedding should not run")
            ):
                context = kh.context_search(
                    self.db, "alpha", "unrelated lexical query", 8, 4, True
                )
        finally:
            kh._EMBEDDING_MODEL_INSTANCE = original_instance
            if original_ready:
                kh._EMBEDDING_MODEL_READY.set()
            else:
                kh._EMBEDDING_MODEL_READY.clear()
        self.assertEqual(context["semantic_mode"], "lexical_fast_path")

    def test_context_stays_lexical_until_first_embedding_inference_finishes(self):
        memory = kh.remember(
            self.db, "alpha", "Warmup race", "warmup-race-marker", "constraint"
        )
        self.db.execute(
            "INSERT OR REPLACE INTO memory_embeddings(document_id,model,dimensions,embedding,content_hash,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                memory["document_id"],
                kh.EMBEDDING_MODEL,
                kh.EMBEDDING_DIMENSIONS,
                b"not-read-before-ready",
                "race-test-hash",
                kh.utcnow(),
            ),
        )
        self.db.commit()
        original_instance = kh._EMBEDDING_MODEL_INSTANCE
        original_ready = kh._EMBEDDING_MODEL_READY.is_set()
        kh._EMBEDDING_MODEL_INSTANCE = object()
        kh._EMBEDDING_MODEL_READY.clear()
        try:
            with mock.patch.object(
                kh, "embed_query", side_effect=AssertionError("first inference is still warming")
            ):
                context = kh.context_search(self.db, "alpha", "unrelated query", 8, 4, True)
        finally:
            kh._EMBEDDING_MODEL_INSTANCE = original_instance
            if original_ready:
                kh._EMBEDDING_MODEL_READY.set()
            else:
                kh._EMBEDDING_MODEL_READY.clear()
        self.assertEqual(context["semantic_mode"], "lexical_fast_path")

    def test_initialize_migrates_legacy_fts_to_project_partition(self):
        db_path = Path(self.tmp.name) / "legacy-fts.sqlite3"
        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            """
            CREATE TABLE chunks (
              id INTEGER PRIMARY KEY,
              document_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              chunk_index INTEGER NOT NULL,
              title TEXT NOT NULL,
              relative_path TEXT,
              content TEXT NOT NULL,
              UNIQUE(document_id, chunk_index)
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
              title, relative_path, content,
              content='chunks', content_rowid='id', tokenize='unicode61'
            );
            CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunks_fts(rowid,title,relative_path,content)
              VALUES(new.id,new.title,new.relative_path,new.content);
            END;
            INSERT INTO chunks(document_id,project_id,chunk_index,title,relative_path,content)
            VALUES('doc-one','project-one',0,'note','note.md','partition-migration-marker');
            """
        )
        legacy.commit()
        legacy.close()

        migrated = kh.connect(db_path)
        kh.initialize(migrated)
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(chunks_fts)")
        }
        self.assertIn("project_id", columns)
        self.assertEqual(
            migrated.execute(
                "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                (kh.scoped_fts_query("partition-migration-marker", ["project-one"]),),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            migrated.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action='index.fts_project_partition_migrated'"
            ).fetchone()[0],
            1,
        )
        migrated.close()

    def test_context_combines_project_and_global_without_cross_project_leak(self):
        other_root = Path(self.tmp.name) / "other"
        other_root.mkdir()
        kh.add_project(self.db, "other", "Other", str(other_root))
        kh.remember(self.db, "alpha", "Alpha", "shared-context-marker alpha-only", "fact")
        kh.remember(self.db, "other", "Other", "shared-context-marker other-secret", "fact")
        kh.remember(self.db, "global-engineering", "Global", "shared-context-marker global-rule", "fact")
        context = kh.context_search(self.db, "alpha", "shared-context-marker", 8, 4, True)
        content = "\n".join(item["content"] for item in context["results"])
        self.assertIn("alpha-only", content)
        self.assertIn("global-rule", content)
        self.assertNotIn("other-secret", content)
        self.assertEqual(context["precedence"][1:4], ["project", "collection", "global"])

    def test_context_separates_candidate_memory_from_trusted_results(self):
        candidate = kh.remember(
            self.db,
            "alpha",
            "Historical candidate",
            "historical-candidate-marker needs confirmation",
            "constraint",
            {"status": "candidate", "capture_mode": "historical_backfill", "confidence": 0.85},
        )
        context = kh.context_search(self.db, "alpha", "historical-candidate-marker")
        self.assertEqual(context["results"], [])
        self.assertEqual(context["result_counts"]["candidates"], 1)
        self.assertEqual(
            context["candidate_memories"][0]["document_id"], candidate["document_id"]
        )
        self.assertTrue(context["candidate_memories"][0]["review_required"])

    def test_hybrid_retrieval_keeps_semantic_memory_in_resolved_project(self):
        created = kh.remember(
            self.db, "alpha", "Release policy", "Run the durable regression suite", "constraint"
        )
        project = kh.get_project(self.db, "alpha")
        fake = {
            "document_id": created["document_id"],
            "title": "Release policy",
            "relative_path": None,
            "content": "Run the durable regression suite",
            "source_type": "memory",
            "source_uri": None,
            "modified_at": kh.utcnow(),
            "project_id": project["id"],
            "scope_key": "alpha",
            "scope_name": "Alpha",
            "scope_type": "project",
            "memory_kind": "constraint",
            "memory_status": "active",
            "memory_confidence": 1.0,
            "memory_evidence": None,
            "semantic_score": 0.77,
            "score": None,
            "retrieval_method": "semantic",
        }
        with mock.patch.object(kh, "semantic_search_memories", return_value=[fake]) as semantic:
            results = kh.hybrid_scope_search(
                self.db, "alpha", "completely different wording", 5
            )
        self.assertEqual([item["document_id"] for item in results], [created["document_id"]])
        self.assertEqual(results[0]["retrieval_method"], "semantic")
        self.assertEqual(semantic.call_args.args[1], [project["id"]])

    def test_web_content_cannot_be_automatically_captured(self):
        with self.assertRaises(ValueError):
            kh.capture_memory(
                self.db, "alpha", "Web claim", "website-claim-marker is true", "fact",
                "网页声称 website-claim-marker", 0.99, source_type="web",
            )

    def test_web_search_uses_working_engine_and_rejects_engine_failure(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "results": [
                    {
                        "title": "Official result",
                        "url": "https://example.com/docs",
                        "content": "Documentation",
                        "engine": "bing",
                    }
                ],
                "unresponsive_engines": [],
            }
        ).encode()
        with (
            mock.patch.dict(kh.os.environ, {"KHUB_SEARXNG_ENGINES": "bing"}),
            mock.patch.object(kh.urllib.request, "urlopen", return_value=response) as opener,
        ):
            result = kh.web_search(self.db, "official docs")
        self.assertEqual(result["engines"], "bing")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["quality"]["raw_result_count"], 1)
        self.assertEqual(result["quality"]["accepted_result_count"], 1)
        self.assertIn("engines=bing", opener.call_args.args[0].full_url)

        failed = mock.MagicMock()
        failed.__enter__.return_value = failed
        failed.read.return_value = json.dumps(
            {"results": [], "unresponsive_engines": [["bing", "timeout"]]}
        ).encode()
        with (
            mock.patch.dict(kh.os.environ, {"KHUB_SEARXNG_ENGINES": "bing"}),
            mock.patch.object(kh.urllib.request, "urlopen", return_value=failed),
        ):
            with self.assertRaisesRegex(ValueError, "搜索引擎当前不可用"):
                kh.web_search(self.db, "unavailable query")

        failure = self.db.execute(
            "SELECT details_json FROM audit_log WHERE action='web.search.failed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(json.loads(failure[0])["reason"], "no_results")

    def test_web_search_retries_with_searxng_default_engines(self):
        unavailable = mock.MagicMock()
        unavailable.__enter__.return_value = unavailable
        unavailable.read.return_value = json.dumps(
            {"results": [], "unresponsive_engines": [["bing", "timeout"]]}
        ).encode()
        recovered = mock.MagicMock()
        recovered.__enter__.return_value = recovered
        recovered.read.return_value = json.dumps(
            {
                "results": [{
                    "title": "Python 3.14 release notes",
                    "url": "https://docs.python.org/3.14/whatsnew/3.14.html",
                    "content": "Official Python 3.14 release notes",
                    "engine": "google",
                }],
                "unresponsive_engines": [],
            }
        ).encode()
        with (
            mock.patch.dict(kh.os.environ, {"KHUB_SEARXNG_ENGINES": "bing"}),
            mock.patch.object(
                kh.urllib.request, "urlopen", side_effect=[unavailable, recovered]
            ) as opener,
        ):
            result = kh.web_search(self.db, "Python 3.14 release notes")

        self.assertTrue(result["quality"]["fallback_used"])
        self.assertEqual(result["quality"]["attempts"][1]["mode"], "default_engines")
        self.assertIn("engines=bing", opener.call_args_list[0].args[0].full_url)
        self.assertNotIn("engines=", opener.call_args_list[1].args[0].full_url)

    def test_web_search_simplifies_query_after_empty_retries(self):
        empty = mock.MagicMock()
        empty.__enter__.return_value = empty
        empty.read.return_value = json.dumps(
            {"results": [], "unresponsive_engines": []}
        ).encode()
        recovered = mock.MagicMock()
        recovered.__enter__.return_value = recovered
        recovered.read.return_value = json.dumps(
            {
                "results": [{
                    "title": "Python packaging guide",
                    "url": "https://packaging.python.org/en/latest/guides/",
                    "content": "Official Python packaging documentation",
                    "engine": "google",
                }],
                "unresponsive_engines": [],
            }
        ).encode()
        with (
            mock.patch.dict(kh.os.environ, {"KHUB_SEARXNG_ENGINES": "bing"}),
            mock.patch.object(
                kh.urllib.request,
                "urlopen",
                side_effect=[empty, empty, recovered],
            ) as opener,
        ):
            result = kh.web_search(
                self.db, 'site:packaging.python.org 请搜索 "Python packaging" 官方资料'
            )

        self.assertTrue(result["quality"]["fallback_used"])
        self.assertEqual(result["quality"]["attempts"][-1]["mode"], "simplified_query")
        final_url = kh.urllib.parse.unquote(opener.call_args_list[-1].args[0].full_url)
        self.assertIn("site:packaging.python.org", final_url)
        self.assertNotIn("请搜索", final_url)

    def test_web_search_rejects_nonempty_but_irrelevant_results(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "results": [
                    {
                        "title": "YouTube",
                        "url": "https://www.youtube.com/",
                        "content": "Enjoy videos and music",
                        "engine": "bing",
                    },
                    {
                        "title": "YouTube Kids",
                        "url": "https://www.youtubekids.com/",
                        "content": "A video app for children",
                        "engine": "bing",
                    },
                ],
                "unresponsive_engines": [["yep", "HTTP 403"]],
            }
        ).encode()
        with mock.patch.object(kh.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "明显无关"):
                kh.web_search(
                    self.db,
                    "中文产品界面文案规范 官方 设计系统 错误提示 按钮 命名",
                )

        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM web_search_cache").fetchone()[0], 0
        )
        failure = self.db.execute(
            "SELECT details_json FROM audit_log WHERE action='web.search.failed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        details = json.loads(failure[0])
        self.assertEqual(details["reason"], "irrelevant_results")
        self.assertEqual(details["raw_result_count"], 2)
        self.assertEqual(details["rejected_result_count"], 2)

    def test_web_search_filters_pollution_and_keeps_relevant_results(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "results": [
                    {
                        "title": "YouTube",
                        "url": "https://www.youtube.com/",
                        "content": "Enjoy videos and music",
                        "engine": "bing",
                    },
                    {
                        "title": "What's New In Python 3.14",
                        "url": "https://docs.python.org/3.14/whatsnew/3.14.html",
                        "content": "Python 3.14 release highlights and notes",
                        "engine": "google",
                    },
                ],
                "unresponsive_engines": [],
            }
        ).encode()
        with mock.patch.object(kh.urllib.request, "urlopen", return_value=response):
            result = kh.web_search(self.db, "Python 3.14 release notes")

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["engine"], "google")
        self.assertIn("python", result["results"][0]["matched_terms"])
        self.assertEqual(result["quality"]["rejected_result_count"], 1)

    def test_web_search_site_operator_requires_matching_domain(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "results": [
                    {
                        "title": "Copied UI writing notes",
                        "url": "https://example.net/ant-design-copy",
                        "content": "中文文案规范",
                        "engine": "google",
                    },
                    {
                        "title": "Ant Design language specification",
                        "url": "https://ant.design/docs/spec/copywriting-cn/",
                        "content": "按钮和错误提示文案",
                        "engine": "google",
                    },
                    {
                        "title": "Ant Design Button",
                        "url": "https://ant.design/components/button/",
                        "content": "按钮文案和错误提示",
                        "engine": "google",
                    },
                ],
                "unresponsive_engines": [],
            }
        ).encode()
        with mock.patch.object(kh.urllib.request, "urlopen", return_value=response):
            result = kh.web_search(
                self.db, "site:ant.design/docs/spec 中文 文案 按钮 错误提示"
            )

        self.assertEqual(len(result["results"]), 1)
        self.assertIn("ant.design", result["results"][0]["url"])

        score, _, _, tier = kh.web_search_result_quality(
            "site:ant.design copywriting guidelines button error message",
            {
                "title": "FAQ - Ant Design",
                "url": "https://ant.design/docs/react/faq/",
                "content": "Frequently asked questions about Ant Design",
            },
        )
        self.assertEqual(score, 0)
        self.assertEqual(tier, "rejected")

    def test_web_search_prefers_primary_sources_and_limits_low_quality_domains(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "results": [
                    {
                        "title": f"产品界面文案设计规范与错误提示示例 {index}",
                        "url": f"{url}/{index}",
                        "content": "按钮文案、错误提示和界面设计规范",
                        "engine": "duckduckgo",
                    }
                    for index, url in enumerate(
                        [
                            "https://zhuanlan.zhihu.com/p",
                            "https://blog.csdn.net/example/article/details",
                            "https://wenku.baidu.com/view",
                            "https://www.toutiao.com/article",
                        ]
                    )
                ]
                + [
                    {
                        "title": "界面用语通用设计规范",
                        "url": "https://developer.huawei.com/consumer/cn/doc/design-guides/ui-language",
                        "content": "产品按钮文案与错误提示规范",
                        "engine": "duckduckgo",
                    }
                ],
                "unresponsive_engines": [],
            }
        ).encode()
        with mock.patch.object(kh.urllib.request, "urlopen", return_value=response):
            result = kh.web_search(
                self.db, "中文产品界面文案规范 设计系统 错误提示 按钮"
            )

        self.assertEqual(result["results"][0]["source_tier"], "primary_candidate")
        self.assertEqual(
            sum(item["source_tier"] == "low_quality" for item in result["results"]), 2
        )
        self.assertEqual(result["quality"]["returned_source_tiers"]["low_quality"], 2)

    def test_memory_update_history_and_soft_delete(self):
        created = kh.remember(self.db, "alpha", "Mutable", "old-memory-marker remains", "decision")
        updated = kh.update_memory(
            self.db, created["document_id"], "new-memory-marker replaces old", "用户纠正旧决策",
            "新规则应为 new-memory-marker", True,
        )
        self.assertEqual(updated["status"], "active")
        self.assertGreaterEqual(updated["versions"], 2)
        self.assertEqual(kh.search(self.db, "alpha", "old-memory-marker"), [])
        self.assertEqual(len(kh.search(self.db, "alpha", "new-memory-marker")), 1)
        deleted = kh.forget_memory(
            self.db, created["document_id"], "用户明确撤销", "不要再记住 new-memory-marker", True,
        )
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(kh.search(self.db, "alpha", "new-memory-marker"), [])
        self.assertTrue(kh.explain_memory(self.db, created["document_id"])["history"])

    def test_conflicting_title_becomes_candidate_until_confirmed(self):
        old = kh.remember(self.db, "alpha", "Deployment policy", "deploy-policy-old-marker", "constraint")
        candidate = kh.remember(self.db, "alpha", "Deployment policy", "deploy-policy-new-marker", "constraint")
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(kh.search(self.db, "alpha", "deploy-policy-new-marker"), [])
        activated = kh.update_memory(
            self.db, candidate["document_id"], "deploy-policy-new-marker", "用户确认新策略替代旧策略",
            "确认 deploy-policy-new-marker 为新策略", True, supersedes_id=old["document_id"],
        )
        self.assertEqual(activated["status"], "active")
        self.assertEqual(kh.get_memory(self.db, old["document_id"])["status"], "superseded")

    def test_memory_can_move_from_project_to_global_with_lineage(self):
        original = kh.remember(self.db, "alpha", "Shared rule", "move-memory-global-marker", "constraint")
        moved = kh.move_memory(
            self.db, original["document_id"], "global", "用户提升为全局规则",
            "以后所有项目都遵守 move-memory-global-marker", True,
        )
        self.assertTrue(moved["moved"])
        self.assertEqual(moved["to"], "global-engineering")
        self.assertEqual(kh.get_memory(self.db, original["document_id"])["status"], "superseded")
        self.assertEqual(len(kh.search(self.db, "global-engineering", "move-memory-global-marker")), 1)

    def test_collection_memory_is_stored_in_virtual_member(self):
        now = kh.utcnow()
        collection_id = "11111111-1111-1111-1111-111111111111"
        self.db.execute(
            "INSERT INTO project_collections(id,slug,display_name,created_at,updated_at) VALUES(?,?,?,?,?)",
            (collection_id, "alpha-suite", "Alpha Suite", now, now),
        )
        alpha_id = kh.get_project(self.db, "alpha")["id"]
        self.db.execute(
            "INSERT INTO collection_members(collection_id,project_id) VALUES(?,?)",
            (collection_id, alpha_id),
        )
        self.db.commit()
        target = kh.ensure_collection_memory_scope(self.db, "collection:alpha-suite")
        kh.remember(self.db, target, "Suite rule", "collection-memory-marker", "runbook")
        results = kh.search(self.db, "collection:alpha-suite", "collection-memory-marker")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["scope_type"], "collection")

    def test_ssrf_guard_rejects_private_addresses(self):
        for url in ("http://127.0.0.1/x", "http://localhost/x", "http://[::1]/x"):
            with self.assertRaises(ValueError):
                kh.validate_public_url(url)

    def test_parent_project_excludes_nested_project_root(self):
        nested = self.root / "child"
        nested.mkdir()
        (self.root / "parent.md").write_text("parent-only-marker", encoding="utf-8")
        (nested / "child.md").write_text("child-only-marker", encoding="utf-8")
        kh.add_project(self.db, "child", "Child", str(nested))
        kh.ingest_project(self.db, "alpha")
        kh.ingest_project(self.db, "child")
        self.assertEqual(kh.search(self.db, "alpha", "child-only-marker"), [])
        self.assertEqual(len(kh.search(self.db, "child", "child-only-marker")), 1)

    def test_project_excludes_knowledge_hub_itself(self):
        service_root = self.root / "outputs" / "knowledge-hub"
        service_root.mkdir(parents=True)
        (service_root / "runtime.md").write_text("self-reference-marker", encoding="utf-8")
        (self.root / "user.md").write_text("ordinary-project-marker", encoding="utf-8")
        original_root = kh.ROOT
        try:
            kh.ROOT = service_root
            stats = kh.ingest_project(self.db, "alpha")
        finally:
            kh.ROOT = original_root
        self.assertEqual(stats.indexed, 1)
        self.assertEqual(kh.search(self.db, "alpha", "self-reference-marker"), [])
        self.assertEqual(len(kh.search(self.db, "alpha", "ordinary-project-marker")), 1)

    def test_project_excludes_disabled_conversation_bridge_runtime(self):
        legacy_runtime = self.root / "outputs" / "conversation-bridge" / "runtime"
        legacy_runtime.mkdir(parents=True)
        (legacy_runtime / "stale-chat.md").write_text("legacy-runtime-marker", encoding="utf-8")
        original_runtime = kh.LEGACY_CONVERSATION_RUNTIME
        try:
            kh.LEGACY_CONVERSATION_RUNTIME = legacy_runtime
            kh.ingest_project(self.db, "alpha")
        finally:
            kh.LEGACY_CONVERSATION_RUNTIME = original_runtime
        self.assertEqual(kh.search(self.db, "alpha", "legacy-runtime-marker"), [])


if __name__ == "__main__":
    unittest.main()
