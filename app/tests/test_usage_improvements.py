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


if __name__ == "__main__":
    unittest.main()
