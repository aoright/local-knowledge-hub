#!/usr/bin/env python3
"""Safely extract durable, user-authored memory candidates from legacy chats.

The legacy bridge remains read-only and disabled.  This command does not mirror
conversations or modify any application's native database.  It only creates
reviewable ``candidate`` memories in Knowledge Hub when ``--apply`` is present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import knowledge_hub as kh


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = ROOT.parent / "conversation-bridge" / "runtime" / "auto-sync"
DEFAULT_REPORT = kh.DATA_ROOT / "historical-memory-backfill.json"

USER_INPUT_BLOCK = re.compile(
    r"(?ms)^###\s+User Input\s*\n(.*?)(?=^###\s+[^\n]+\s*$|\Z)"
)
RULE_MARKERS = re.compile(
    r"(?:以后|今后|长期|必须|务必|不要|禁止|不允许|不能|一律|始终|永远|"
    r"不需要|默认|固定|统一|保持|只允许|只能|优先|"
    r"我(?:决定|确定|要求|希望默认|偏好)|我的目标是|"
    r"all\s+projects|always|must|never|do\s+not|don't|by\s+default|"
    r"we\s+(?:decided|require)|i\s+(?:prefer|require))",
    re.IGNORECASE,
)
QUESTION_START = re.compile(
    r"^(?:为什么|为何|怎么|如何|是否|能否|可否|请问|有没有|有无|哪(?:个|些)|什么|谁|何时|"
    r"why\b|how\b|what\b|which\b|who\b|when\b|can\b|could\b|should\b|is\b|are\b)",
    re.IGNORECASE,
)
TRANSIENT_ONLY = re.compile(
    r"^(?:继续|已退出|都退出了|已重启|restart(?:ed)?|接下来做什么|检查一下|"
    r"开始|停止|好的|可以|确认|完成|下一步)[。.!！\s]*$",
    re.IGNORECASE,
)
LOG_OR_CODE = re.compile(
    r"(?:```|Traceback \(most recent call last\)|Exception:|Error:|\b(?:GET|POST|PUT|DELETE) /|"
    r"^\s*(?:\$|#)\s+\S+|^\s*(?:SELECT|INSERT|UPDATE|CREATE TABLE)\b)",
    re.IGNORECASE | re.MULTILINE,
)
TASK_REQUEST = re.compile(
    r"^(?:请|帮我|麻烦|搜索全网|检查一下|查看|读取|分析|修复|修改|创建|执行|运行|打开|关闭|"
    r"please\b|help\b|search\b|check\b|inspect\b|run\b|execute\b)",
    re.IGNORECASE,
)
EXPLICIT_DURABLE = re.compile(
    r"^(?:(?:以后|今后|长期|所有项目|全部项目|任何项目|每个项目|全局).{0,40}"
    r"(?:必须|务必|不要|禁止|不允许|不能|不需要|一律|始终|默认|固定|统一|保持|只允许|只能)|"
    r"(?:不要|禁止|必须|务必|默认|固定|统一|保持|只允许|只能)|"
    r"我(?:决定|确定|要求|希望默认|偏好)|我的目标是|"
    r"(?:all\s+projects|always|by\s+default|we\s+decided|i\s+(?:prefer|require)))",
    re.IGNORECASE,
)
TRANSIENT_CONTEXT = re.compile(
    r"(?:现在|目前|暂时|这次|刚才|刚刚|还是|已经|先不要|先在|继续|试一下|"
    r"卡住|报错|失败|没有反应|没有变化|不能用|打不开|测试一下|帮我修复|帮我检查)",
    re.IGNORECASE,
)
ASSISTANT_LIKE = re.compile(
    r"^(?:模块|实际|原因|起因|第一步|第二步|方案\s*[A-Z一二三四]|阶段\s*\d+|"
    r"User\s*:|Assistant\s*:|Planner\s*:|\*?FAIL\b)",
    re.IGNORECASE,
)
STABLE_FACT_MARKERS = re.compile(
    r"(?:默认|固定|长期|始终|我确定|我的目标是|事实是|所有项目|全部项目|全局|"
    r"by\s+default|always|i\s+confirm)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Candidate:
    conversation_id: str
    message_index: int
    created_at: str | None
    source_project_root: str | None
    target_scope: str
    title: str
    content: str
    kind: str
    confidence: float
    source_hash: str


def normalized_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def user_messages(records: Any) -> Iterable[tuple[int, str, str | None]]:
    if not isinstance(records, list):
        return
    output_index = 0
    for record in records:
        if not isinstance(record, dict) or str(record.get("role", "")).lower() != "user":
            continue
        content = normalized_text(str(record.get("content") or ""))
        created_at = record.get("created_at")
        blocks = USER_INPUT_BLOCK.findall(content)
        values = blocks if blocks else [content]
        for value in values:
            value = normalized_text(value)
            if value:
                yield output_index, value, str(created_at) if created_at else None
                output_index += 1


def statement_units(message: str) -> Iterable[str]:
    """Yield compact prose units while avoiding pasted transcripts and code."""
    message = re.sub(r"(?m)^\s*(?:[-*•]|\d+[、.)．])\s*", "", message)
    paragraphs = re.split(r"\n{2,}|(?<=[。！？!?；;])\s+", message)
    for paragraph in paragraphs:
        paragraph = normalized_text(paragraph)
        if not paragraph:
            continue
        lines = [normalized_text(line) for line in paragraph.splitlines() if normalized_text(line)]
        values = lines if len(lines) > 1 else [paragraph]
        for value in values:
            value = re.sub(r"^#{1,6}\s+", "", value).strip()
            if value:
                yield value


def rejection_reason(statement: str) -> str | None:
    if len(statement) < 8:
        return "too_short"
    if len(statement) > 1_000:
        return "too_long"
    if TRANSIENT_ONLY.match(statement):
        return "transient"
    if LOG_OR_CODE.search(statement):
        return "log_or_code"
    if QUESTION_START.match(statement) or re.search(r"(?:为什么|怎么|如何|是否|能否|可否|有没有|要不要|[?？吗])", statement):
        return "question"
    redacted, redactions = kh.redact_secrets(statement)
    if redactions or "[REDACTED_SECRET]" in redacted:
        return "sensitive"
    if not RULE_MARKERS.search(statement):
        return "no_durable_marker"
    if ASSISTANT_LIKE.match(statement):
        return "assistant_like"
    if TRANSIENT_CONTEXT.search(statement) and not re.match(r"^(?:以后|今后|长期|所有项目|全部项目|全局)", statement):
        return "transient_context"
    if not EXPLICIT_DURABLE.search(statement):
        return "not_explicit"
    if TASK_REQUEST.match(statement) and not re.search(r"(?:以后|今后|长期|默认|一律|始终|所有项目|全部项目)", statement):
        return "one_off_request"
    if sum(char in "{}[]<>`$" for char in statement) > max(8, len(statement) // 12):
        return "code_dense"
    return None


def classify_kind(statement: str) -> str:
    if re.search(r"(?:步骤|流程|操作顺序|发布前|部署前|回滚|备份|恢复|runbook)", statement, re.IGNORECASE):
        return "runbook"
    if re.search(r"(?:决定|确定|采用|选用|统一使用|我的目标是|we decided)", statement, re.IGNORECASE):
        return "decision"
    if re.search(r"(?:必须|务必|不要|禁止|不允许|不能|不需要|只允许|只能|一律|must|never|do not|don't)", statement, re.IGNORECASE):
        return "constraint"
    return "fact"


def candidate_title(statement: str) -> str:
    title = re.sub(r"^[\s:：,，;；。.!！]+", "", statement)
    title = re.sub(r"\s+", " ", title)
    if len(title) > 80:
        title = title[:79].rstrip() + "…"
    return title


def confidence_for(statement: str) -> float:
    score = 0.76
    if re.search(r"(?:以后|今后|长期|一律|始终|所有项目|全部项目|always|all projects)", statement, re.IGNORECASE):
        score += 0.08
    if re.search(r"(?:必须|禁止|不允许|我决定|我确定|we decided|must|never)", statement, re.IGNORECASE):
        score += 0.05
    return min(score, 0.89)


def resolve_target(db: Any, project_root: str | None, statement: str) -> str | None:
    if kh.GLOBAL_SCOPE_MARKERS.search(statement) or kh.USER_PREFERENCE_MARKERS.search(statement):
        return kh.classify_global_scope(statement)
    if not project_root:
        return None
    try:
        return kh.resolve_project_reference(db, workspace_path=project_root)
    except ValueError:
        return None


def iter_candidates(
    db: Any,
    bridge_root: Path,
    report: dict[str, Any],
) -> Iterable[Candidate]:
    registry_path = bridge_root / "sync-registry.json"
    canonical_dir = bridge_root / "canonical"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    conversations = registry.get("conversations", {})
    if not isinstance(conversations, dict):
        raise ValueError("旧会话登记表格式无效")
    report["registered_conversations"] = len(conversations)
    for conversation_id, metadata in conversations.items():
        path = canonical_dir / f"{conversation_id}.json"
        if not path.is_file():
            report["rejected"]["missing_canonical"] += 1
            continue
        report["canonical_files"] += 1
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report["rejected"]["invalid_canonical"] += 1
            continue
        project_root = metadata.get("project_root") if isinstance(metadata, dict) else None
        for message_index, message, created_at in user_messages(records):
            report["user_messages"] += 1
            if len(message) > 1_000:
                report["rejected"]["oversized_user_message"] += 1
                continue
            nonempty_lines = [line for line in message.splitlines() if line.strip()]
            if len(nonempty_lines) > 12 or re.search(r"(?m)^\s*\|.+\|\s*$", message):
                report["rejected"]["structured_paste"] += 1
                continue
            for statement in statement_units(message):
                report["statements_seen"] += 1
                reason = rejection_reason(statement)
                if reason:
                    report["rejected"][reason] += 1
                    continue
                kind = classify_kind(statement)
                if kind == "fact" and not STABLE_FACT_MARKERS.search(statement):
                    report["rejected"]["weak_fact"] += 1
                    continue
                target = resolve_target(db, str(project_root) if project_root else None, statement)
                if not target:
                    report["rejected"]["unresolved_project"] += 1
                    continue
                digest = hashlib.sha256(
                    f"{conversation_id}\0{message_index}\0{statement}".encode("utf-8")
                ).hexdigest()
                yield Candidate(
                    conversation_id=conversation_id,
                    message_index=message_index,
                    created_at=created_at,
                    source_project_root=str(project_root) if project_root else None,
                    target_scope=target,
                    title=candidate_title(statement),
                    content=statement,
                    kind=kind,
                    confidence=confidence_for(statement),
                    source_hash=digest,
                )


def run(db_path: Path, bridge_root: Path, apply: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "bridge_root": str(bridge_root),
        "registered_conversations": 0,
        "canonical_files": 0,
        "user_messages": 0,
        "statements_seen": 0,
        "candidates": 0,
        "inserted": 0,
        "already_existed": 0,
        "by_scope": Counter(),
        "by_kind": Counter(),
        "rejected": Counter(),
    }
    db = kh.connect(db_path)
    kh.initialize(db)
    try:
        for candidate in iter_candidates(db, bridge_root, report):
            report["candidates"] += 1
            report["by_scope"][candidate.target_scope] += 1
            report["by_kind"][candidate.kind] += 1
            if not apply:
                continue
            result = kh.remember(
                db,
                candidate.target_scope,
                candidate.title,
                candidate.content,
                candidate.kind,
                {
                    "status": "candidate",
                    "capture_mode": "historical_backfill",
                    "confidence": candidate.confidence,
                    "evidence": candidate.content,
                    "source_type": "historical_user_statement",
                    "source_conversation_id": candidate.conversation_id,
                    "source_message_index": candidate.message_index,
                    "source_message_hash": candidate.source_hash,
                    "source_project_root": candidate.source_project_root,
                    "valid_from": candidate.created_at or kh.utcnow(),
                    "created_by": "user",
                    "reason": "从旧会话中提取，等待用户确认有效性与作用域",
                },
            )
            key = "already_existed" if result.get("already_existed") else "inserted"
            report[key] += 1
    finally:
        db.close()
    report["by_scope"] = dict(report["by_scope"].most_common())
    report["by_kind"] = dict(report["by_kind"].most_common())
    report["rejected"] = dict(report["rejected"].most_common())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="从旧会话安全回填候选长期记忆")
    parser.add_argument("--db", type=Path, default=kh.DEFAULT_DB)
    parser.add_argument("--bridge-root", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true", help="实际写入候选记忆；默认只统计")
    args = parser.parse_args()
    value = run(args.db, args.bridge_root, args.apply)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.chmod(0o600)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
