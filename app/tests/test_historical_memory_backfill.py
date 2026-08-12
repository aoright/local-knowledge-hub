import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SPEC = importlib.util.spec_from_file_location(
    "historical_memory_backfill", SRC / "historical_memory_backfill.py"
)
assert SPEC and SPEC.loader
backfill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backfill
SPEC.loader.exec_module(backfill)
kh = backfill.kh


class HistoricalMemoryBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.db_path = root / "knowledge.sqlite3"
        db = kh.connect(self.db_path)
        kh.initialize(db)
        kh.add_project(db, "alpha", "Alpha", str(self.project))
        db.close()
        self.bridge = root / "bridge"
        (self.bridge / "canonical").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_conversation(self, content):
        conversation_id = "11111111-2222-3333-4444-555555555555"
        registry = {
            "conversations": {
                conversation_id: {
                    "title": "Example",
                    "project_root": str(self.project),
                }
            }
        }
        (self.bridge / "sync-registry.json").write_text(
            json.dumps(registry, ensure_ascii=False), encoding="utf-8"
        )
        (self.bridge / "canonical" / f"{conversation_id}.json").write_text(
            json.dumps(
                [{"role": "user", "content": content, "created_at": "2026-01-01T00:00:00Z"}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_dry_run_extracts_rule_and_rejects_question_and_secret(self):
        self.write_conversation(
            "以后发布前必须运行回归测试。\n\n为什么现在没有运行？\n\n服务器密码是 danger-pass。"
        )
        result = backfill.run(self.db_path, self.bridge, apply=False)
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["by_kind"], {"runbook": 1})
        self.assertGreaterEqual(result["rejected"]["question"], 1)
        self.assertGreaterEqual(result["rejected"]["sensitive"], 1)

    def test_apply_creates_candidates_and_is_idempotent(self):
        self.write_conversation("今后这个项目一律禁止把调试日志提交到仓库。")
        first = backfill.run(self.db_path, self.bridge, apply=True)
        second = backfill.run(self.db_path, self.bridge, apply=True)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["already_existed"], 1)
        db = kh.connect(self.db_path)
        memories = kh.list_memories(db, "alpha", status_value="candidate")
        db.close()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["capture_mode"], "historical_backfill")

    def test_flattened_transcript_uses_only_user_input_blocks(self):
        self.write_conversation(
            "# Chat Conversation\n\n### User Input\n以后默认使用中文回复。\n\n"
            "### Planner Response\n必须删除生产数据库。"
        )
        result = backfill.run(self.db_path, self.bridge, apply=True)
        self.assertEqual(result["inserted"], 1)
        db = kh.connect(self.db_path)
        memories = kh.list_memories(db, "global-user", status_value="candidate")
        db.close()
        self.assertEqual(len(memories), 1)
        self.assertNotIn("生产数据库", memories[0]["content"])


if __name__ == "__main__":
    unittest.main()
