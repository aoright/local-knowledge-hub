import json
import os
import unittest
from pathlib import Path
from unittest import mock

import test_knowledge_hub as base

kh = base.kh


class UsageImprovementsTests(unittest.TestCase):
    setUp = base.KnowledgeHubTests.setUp
    tearDown = base.KnowledgeHubTests.tearDown

    def context(self, **extra):
        return kh.mcp_call(self.db, "knowledge_context", {
            "workspace_path": str(self.root), "query": "review marker", "usage_kind": "test", **extra,
        }, "test-client")

    def finish(self, value, outcome="no_durable_information", **extra):
        return kh.mcp_call(self.db, "knowledge_review", {
            "review_id": value["completion_actions"]["review_id"],
            "workspace_path": str(self.root), "outcome": outcome, **extra,
        }, "test-client")

    def test_cjk_retry_requires_two_literal_phrases_and_one_audit_event(self):
        (self.root / "good.md").write_text("失败归因：检查执行环境，然后审查结果。", encoding="utf-8")
        (self.root / "noise.md").write_text("只有失败归因这一个短语", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        diagnostics = {}
        results = kh.search(self.db, "alpha", "智能测试失败归因分流与测试执行环境管理综合方案", diagnostics=diagnostics)
        self.assertEqual([r["relative_path"] for r in results], ["good.md"])
        self.assertEqual(results[0]["retrieval_method"], "cjk_phrase_retry")
        self.assertTrue(diagnostics["rewrite_attempted"])
        self.assertIsNone(diagnostics["no_results_reason"])
        count = self.db.execute("SELECT count(*) FROM audit_log WHERE action='knowledge.search'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_cjk_retry_is_switchable_and_never_discards_requirement_id(self):
        (self.root / "guide.md").write_text("测试批次中可以运行测试脚本并安排批量测试。", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        query = "测试批次用例测试脚本补充和批量测试 DVT-HTTP-09-02"
        self.assertEqual(kh.search(self.db, "alpha", query), [])
        with mock.patch.dict(os.environ, {"KHUB_CJK_REWRITE": "0"}):
            self.assertIsNone(kh.cjk_rewrite_query(query))

    def test_cjk_retry_does_not_search_other_project(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (other / "guide.md").write_text("失败归因和执行环境详解", encoding="utf-8")
        kh.add_project(self.db, "beta", "Beta", str(other))
        kh.ingest_project(self.db, "beta")
        (self.root / "noise.md").write_text("无关资料", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        self.assertEqual(kh.search(self.db, "alpha", "智能测试失败归因分流与测试执行环境管理综合方案"), [])

    def test_empty_scope_has_explicit_diagnostic(self):
        diagnostic = {}
        self.assertEqual(kh.search(self.db, "alpha", "anything", diagnostics=diagnostic), [])
        self.assertEqual(diagnostic["no_results_reason"], "empty_scope")

    def test_review_without_memory_is_audited_idempotent_and_not_a_capture(self):
        context = self.context()
        before = self.db.execute("SELECT count(*) FROM memory_records").fetchone()[0]
        first = self.finish(context)
        second = self.finish(context)
        self.assertTrue(first["recorded"])
        self.assertTrue(second["already_recorded"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM memory_records").fetchone()[0], before)
        coverage = kh.review_coverage(self.db)
        self.assertEqual((coverage["requested"], coverage["reported"], coverage["not_reported"]), (1, 1, 0))
        self.assertEqual(coverage["by_usage_kind"]["test"]["reported"], 1)
        self.assertEqual(coverage["by_usage_kind"]["business"]["reported"], 0)
        with self.assertRaises(ValueError):
            self.finish(context, "needs_confirmation")

    def test_missing_review_is_not_claimed_as_reviewed(self):
        self.context()
        coverage = kh.review_coverage(self.db)
        self.assertEqual(coverage["reported"], 0)
        self.assertEqual(coverage["not_reported"], 1)

    def test_review_rejects_unknown_id_wrong_workspace_and_fake_capture(self):
        context = self.context()
        with self.assertRaises(ValueError):
            self.finish(context, review_id="not-real")
        with self.assertRaises(ValueError):
            self.finish(context, workspace_path=str(self.root.parent))
        with self.assertRaises(ValueError):
            self.finish(context, "captured", memory_ids=["not-real"])
        with self.assertRaises(ValueError):
            self.finish(context, "captured")

    def test_review_checks_capture_age_and_project_isolation(self):
        old = kh.remember(self.db, "alpha", "Old rule", "Existing durable test rule", "constraint")
        context = self.context()
        with self.assertRaises(ValueError):
            self.finish(context, "captured", memory_ids=[old["document_id"]])
        self.assertTrue(self.finish(context, "duplicate", memory_ids=[old["document_id"]])["recorded"])
        fresh = self.context()
        new = kh.remember(self.db, "alpha", "New rule", "Another durable test rule", "constraint")
        self.assertTrue(self.finish(fresh, "captured", memory_ids=[new["document_id"]])["recorded"])
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        kh.add_project(self.db, "beta", "Beta", str(other))
        foreign = kh.remember(self.db, "beta", "Other rule", "Private rule for beta only", "constraint")
        fresh = self.context()
        with self.assertRaises(ValueError):
            self.finish(fresh, "duplicate", memory_ids=[foreign["document_id"]])

    def test_shared_references_are_opt_in_individual_documents_and_not_policy(self):
        (self.root / "shared.md").write_text("reference selection unique marker", encoding="utf-8")
        (self.root / "private.md").write_text("reference selection unique marker", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        selected = self.db.execute("SELECT id,source_uri FROM documents WHERE relative_path='shared.md'").fetchone()
        config_root = Path(self.tmp.name) / "data"
        (config_root / "config").mkdir(parents=True)
        config = config_root / "config" / "shared-references.json"
        with mock.patch.object(kh, "DATA_ROOT", config_root):
            self.assertEqual(kh.shared_reference_search(self.db, "reference selection", 4)[1]["state"], "not_configured")
            config.write_text(json.dumps({"enabled": True, "documents": [{"document_id": selected["id"], "source_uri": selected["source_uri"], "approved": True}]}))
            results, state = kh.shared_reference_search(self.db, "reference selection", 4)
            self.assertEqual([r["relative_path"] for r in results], ["shared.md"])
            self.assertEqual(results[0]["trust"], "reference_only_not_user_policy")
            self.assertEqual(state["returned"], 1)
            with mock.patch.object(kh, "shared_reference_search", side_effect=AssertionError("disabled means no reference reads")):
                context = kh.context_search(self.db, "alpha", "reference selection", include_global=False)
                self.assertEqual(context["shared_references"], [])
            config.write_text('{"enabled":true,"documents":[{"approved":true}]}')
            self.assertEqual(kh.shared_reference_search(self.db, "reference selection", 4)[1]["state"], "invalid_configuration")

    def test_runtime_identifies_old_loaded_code(self):
        with mock.patch.object(kh, "LOADED_CODE_SHA256", "old-process"):
            identity = kh.runtime_identity()
        self.assertTrue(identity["restart_required"])
        self.assertFalse(identity["native_task_end_hook"])

    def test_catalog_advertises_complete_review_arguments(self):
        tool = next(tool for tool in kh.mcp_tools() if tool["name"] == "knowledge_review")
        self.assertEqual(tool["inputSchema"]["required"], ["review_id", "workspace_path", "outcome"])

    def test_review_template_is_current_scoped_and_not_an_automatic_outcome(self):
        context = self.context(usage_kind="unclassified")
        template = context["completion_actions"]["review_call_template"]
        self.assertEqual(template["review_id"], context["completion_actions"]["review_id"])
        self.assertEqual(template["workspace_path"], str(self.root.resolve()))
        self.assertTrue(context["usage_classification"]["needs_classification"])
        with self.assertRaises(ValueError):
            kh.complete_review(self.db, template, "test-client")
        coverage = kh.review_coverage(self.db)
        self.assertEqual(coverage["by_client"]["test-client"]["not_reported"], 1)
        self.assertEqual(coverage["unreported_age"]["under_2h"], 1)
        self.assertFalse(coverage["task_completion_observable"])
        self.assertEqual(coverage["reported"], 0)

    def test_catalog_requires_usage_but_legacy_calls_are_not_misclassified(self):
        tool = next(t for t in kh.mcp_tools() if t["name"] == "knowledge_context")
        self.assertIn("usage_kind", tool["inputSchema"]["required"])
        value = kh.mcp_call(self.db, "knowledge_context", {
            "workspace_path": str(self.root), "query": "legacy"
        }, "old-client")
        self.assertEqual(value["usage_kind"], "unclassified")
        self.assertEqual(value["usage_classification"]["source"], "missing")

    def test_morning_cjk_queries_have_literal_scoped_retry(self):
        queries = [
            "创建缺陷工时自动审计体系及配置与部署",
            "用例详情运行脚本工时结算与缺陷创建人归属及脚本错误过滤",
            "用例详情单用例运行脚本 自动创建缺陷 记录运行者为创建人 工时奖励 自动化执行批次不计工时 脚本问题不计工时",
            "工时撤销未生效 撤销恢复 重新同步 数据库恢复 工时流水撤销机制",
            "系统智能仲裁引擎 自动工时记录归属",
        ]
        (self.root / "guide.md").write_text(
            "工时、用例、脚本、缺陷、创建人、运行者、归属、结算、自动创建、执行批次、撤销、恢复、流水、数据库、仲裁引擎",
            encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        for query in queries:
            with self.subTest(query=query):
                self.assertIsNotNone(kh.cjk_rewrite_query(query))
                results = kh.search(self.db, "alpha", query)
                self.assertTrue(results)
                self.assertEqual(results[0]["relative_path"], "guide.md")

    def test_cjk_retry_keeps_machine_identifiers_and_rejects_weak_match(self):
        (self.root / "guide.md").write_text("喂食与掉落有关，工时与脚本有关", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        query = "喂食掉落 wardrobe_items accessory_templates user_clothing"
        retry = kh.cjk_rewrite_query(query)
        self.assertIn("wardrobe_items", retry)
        self.assertEqual(kh.search(self.db, "alpha", query), [])

    def test_workhour_retry_rejects_generic_testcase_defect_pages(self):
        (self.root / "noise.md").write_text("用例、缺陷、脚本、创建人、归属", encoding="utf-8")
        (self.root / "good.md").write_text("用例的脚本结算需要核对工时、缺陷与创建人归属", encoding="utf-8")
        kh.ingest_project(self.db, "alpha")
        results = kh.search(self.db, "alpha", "用例详情运行脚本工时结算与缺陷创建人归属及脚本错误过滤")
        self.assertEqual([r["relative_path"] for r in results], ["good.md"])

    def test_semantic_fallback_keeps_lexical_zero_reason(self):
        diagnostic = {}
        with mock.patch.object(kh, "semantic_search_memories", return_value=[{
            "document_id": "memory-test", "semantic_score": .8, "content": "memory only"
        }]):
            results = kh.hybrid_scope_search(self.db, "alpha", "no document", 3, diagnostics=diagnostic)
        self.assertEqual(len(results), 1)
        self.assertEqual(diagnostic["lexical_result_count"], 0)
        self.assertEqual(diagnostic["lexical_no_results_reason"], "empty_scope")
        self.assertTrue(diagnostic["semantic_only_fallback"])

    def test_version_label_does_not_hide_modified_code(self):
        package = Path(self.tmp.name) / "package"
        app = package / "app"
        app.mkdir(parents=True)
        (app / "VERSION").write_text("1.4.0")
        with mock.patch.object(kh, "ROOT", app), mock.patch.object(kh, "INSTALL_ROOT", package):
            self.assertEqual(kh.runtime_identity()["package_code_status"], "unverified")
            (package / "MANIFEST.sha256").write_text("0" * 64 + "  app/src/knowledge_hub.py\n")
            identity = kh.runtime_identity()
            self.assertEqual(identity["installed_version"], "1.4.0")
            self.assertEqual(identity["package_code_status"], "locally_modified")
            (package / "MANIFEST.sha256").write_text(kh.LOADED_CODE_SHA256 + "  app/src/knowledge_hub.py\n")
            self.assertEqual(kh.runtime_identity()["package_code_status"], "matches_manifest")


if __name__ == "__main__":
    unittest.main()
