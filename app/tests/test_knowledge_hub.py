import importlib.util
import json
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

    def test_secret_file_is_excluded_and_inline_secret_redacted(self):
        (self.root / ".env").write_text("PASSWORD=should-never-index", encoding="utf-8")
        (self.root / "config.md").write_text("api_key=abcdefghijklmnop visible-text", encoding="utf-8")
        stats = kh.ingest_project(self.db, "alpha")
        self.assertGreaterEqual(stats.secret_redactions, 1)
        self.assertEqual(kh.search(self.db, "alpha", "should-never-index"), [])
        result = kh.search(self.db, "alpha", "visible-text")
        self.assertIn("[REDACTED_SECRET]", result[0]["content"])

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

    def test_mcp_tool_catalog_is_json_serializable(self):
        tools = kh.mcp_tools()
        encoded = json.dumps(tools)
        self.assertIn('"knowledge_search"', encoded)
        remember_tool = next(tool for tool in tools if tool["name"] == "knowledge_remember")
        self.assertIs(remember_tool["inputSchema"]["properties"]["confirmed"]["const"], True)
        self.assertIn("knowledge_context", [tool["name"] for tool in tools])
        self.assertIn("knowledge_capture", [tool["name"] for tool in tools])
        self.assertEqual(len(tools), 13)
        self.assertIn("knowledge_update", [tool["name"] for tool in tools])
        self.assertIn("knowledge_forget", [tool["name"] for tool in tools])
        self.assertIn("knowledge_move", [tool["name"] for tool in tools])

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
