#!/usr/bin/env python3
"""Backup, restore validation, automatic indexing, and health checks."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import knowledge_hub as kh


ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = kh.DATA_ROOT / "backups"
LOCK_FILE = kh.DATA_ROOT / "maintenance.lock"
INDEX_STATE_FILE = kh.DATA_ROOT / "index-state.json"
FULL_SCAN_INTERVAL_SECONDS = 24 * 60 * 60
CRITICAL_TABLES = (
    ("projects", ""),
    ("project_paths", ""),
    ("project_aliases", ""),
    ("project_collections", ""),
    ("collection_members", ""),
    ("documents", "WHERE source_type='memory'"),
    (
        "chunks",
        "WHERE document_id IN (SELECT id FROM documents WHERE source_type='memory')",
    ),
    ("memory_records", ""),
    ("memory_history", ""),
    ("memory_embeddings", ""),
    ("audit_log", ""),
)


def checkpoint_database(
    db_path: Path | None = None,
    truncate: bool = False,
    busy_timeout_ms: int = 2_000,
) -> dict[str, Any]:
    """Checkpoint WAL without waiting indefinitely for active MCP readers."""
    path = (db_path or kh.DEFAULT_DB).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"数据库不存在：{path}")
    wal_path = Path(f"{path}-wal")
    before = wal_path.stat().st_size if wal_path.exists() else 0
    db = sqlite3.connect(path, timeout=max(0.1, busy_timeout_ms / 1_000))
    try:
        db.execute(f"PRAGMA busy_timeout={max(0, int(busy_timeout_ms))}")
        mode = "TRUNCATE" if truncate else "PASSIVE"
        busy, log_frames, checkpointed_frames = db.execute(
            f"PRAGMA wal_checkpoint({mode})"
        ).fetchone()
    finally:
        db.close()
    after = wal_path.stat().st_size if wal_path.exists() else 0
    return {
        "database": str(path),
        "mode": mode.lower(),
        "busy": int(busy),
        "log_frames": int(log_frames),
        "checkpointed_frames": int(checkpointed_frames),
        "wal_bytes_before": before,
        "wal_bytes_after": after,
        "truncated": bool(truncate and not busy and after < before),
    }


def prune_backups(
    full_retain: int = 3,
    critical_retain: int = 14,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply exact, mode-aware backup retention targets."""
    if full_retain < 1 or critical_retain < 1:
        raise ValueError("每种备份至少保留 1 份")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    groups = {
        "full": (
            sorted(BACKUP_DIR.glob("knowledge-hub-[0-9]*.sqlite3.gz"), reverse=True),
            full_retain,
        ),
        "critical": (
            sorted(BACKUP_DIR.glob("knowledge-hub-critical-*.sqlite3.gz"), reverse=True),
            critical_retain,
        ),
    }
    remove: list[Path] = []
    retained: dict[str, int] = {}
    for name, (files, keep) in groups.items():
        retained[name] = min(len(files), keep)
        remove.extend(files[keep:])
    reclaimable = sum(
        path.stat().st_size
        + (
            path.with_suffix(path.suffix + ".sha256").stat().st_size
            if path.with_suffix(path.suffix + ".sha256").exists()
            else 0
        )
        for path in remove
    )
    if apply:
        for path in remove:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".sha256").unlink(missing_ok=True)
    return {
        "applied": apply,
        "full_retain": full_retain,
        "critical_retain": critical_retain,
        "retained": retained,
        "remove_count": len(remove),
        "reclaimable_bytes": reclaimable,
        "targets": [str(path) for path in remove],
    }


def review_automatic_memories(
    apply: bool = False,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Preview or quarantine noisy automatic memories without deleting them."""
    db = kh.connect(db_path or kh.DEFAULT_DB)
    kh.initialize(db)
    rows = db.execute(
        "SELECT mr.document_id,mr.scope_type,mr.kind,mr.evidence,d.title,p.slug "
        "FROM memory_records mr JOIN documents d ON d.id=mr.document_id "
        "JOIN projects p ON p.id=d.project_id "
        "WHERE mr.status='active' AND mr.capture_mode='automatic' "
        "ORDER BY mr.created_at"
    ).fetchall()
    targets: list[dict[str, Any]] = []
    for row in rows:
        issues = kh.automatic_memory_evidence_issues(
            row["evidence"] or "", row["kind"], row["scope_type"]
        )
        if issues:
            targets.append({
                "document_id": row["document_id"],
                "scope": row["slug"],
                "title": row["title"],
                "issues": issues,
            })
    if apply:
        now = kh.utcnow()
        for target in targets:
            memory = kh.get_memory(db, target["document_id"])
            metadata = dict(memory["metadata"])
            metadata["status"] = "candidate"
            reason = "自动记忆质量复核：" + "、".join(target["issues"])
            kh.append_memory_history(
                db, memory["id"], "quality_quarantined", memory["title"],
                memory["content"], metadata, reason, memory.get("evidence"), "system",
            )
            db.execute(
                "UPDATE memory_records SET status='candidate',updated_at=? WHERE document_id=?",
                (now, memory["id"]),
            )
            db.execute(
                "UPDATE documents SET metadata_json=?,indexed_at=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), now, memory["id"]),
            )
            kh.audit(
                db, "memory.quality_quarantined", memory["project_id"],
                {"document_id": memory["id"], "issues": target["issues"]},
            )
        db.commit()
    db.close()
    return {
        "applied": apply,
        "review_count": len(targets),
        "action": "demote_to_candidate",
        "targets": targets,
    }


def lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.chmod(0o700)
    handle = LOCK_FILE.open("a+b")
    LOCK_FILE.chmod(0o600)
    # Scheduled indexing and the daily backup may occasionally overlap. Queue
    # maintenance jobs instead of failing a once-per-day backup immediately.
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def copy_table(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    where: str = "",
) -> int:
    columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
    names = ",".join(columns)
    placeholders = ",".join("?" for _ in columns)
    rows = source.execute(f"SELECT {names} FROM {table} {where}").fetchall()
    if rows:
        target.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})", rows
        )
    return len(rows)


def critical_snapshot(source: sqlite3.Connection, target: sqlite3.Connection) -> dict:
    """Create a small, rebuildable snapshot without first copying the full DB."""
    kh.initialize(target)
    for table in (
        "memory_embeddings",
        "memory_history",
        "memory_records",
        "chunks",
        "documents",
        "collection_members",
        "project_aliases",
        "project_paths",
        "project_collections",
        "projects",
        "audit_log",
    ):
        target.execute(f"DELETE FROM {table}")
    source.execute("BEGIN")
    try:
        copied = {
            table: copy_table(source, target, table, where)
            for table, where in CRITICAL_TABLES
        }
        target.commit()
    finally:
        source.rollback()
    return copied


def backup(retain: int = 14, mode: str = "full") -> dict:
    if mode not in {"full", "critical"}:
        raise ValueError("备份模式必须是 full 或 critical")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = "knowledge-hub-critical" if mode == "critical" else "knowledge-hub"
    destination = BACKUP_DIR / f"{prefix}-{stamp}.sqlite3.gz"
    with tempfile.TemporaryDirectory(dir=BACKUP_DIR) as temp_dir:
        snapshot = Path(temp_dir) / "snapshot.sqlite3"
        source_db = kh.connect()
        source_path_text = source_db.execute("PRAGMA database_list").fetchone()[2]
        target_db = sqlite3.connect(snapshot)
        target_db.row_factory = sqlite3.Row
        if mode == "critical":
            copied = critical_snapshot(source_db, target_db)
        else:
            source_db.backup(target_db)
            copied = {}
        target_db.close()
        source_db.close()
        check_db = sqlite3.connect(snapshot)
        try:
            check = check_db.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check_db.close()
        if check != "ok":
            raise RuntimeError(f"备份完整性检查失败：{check}")
        with snapshot.open("rb") as source, gzip.open(destination, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target)
    destination.chmod(0o600)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checksum = destination.with_suffix(destination.suffix + ".sha256")
    checksum.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    checksum.chmod(0o600)
    pattern = (
        "knowledge-hub-critical-*.sqlite3.gz"
        if mode == "critical"
        else "knowledge-hub-[0-9]*.sqlite3.gz"
    )
    backups = sorted(BACKUP_DIR.glob(pattern), reverse=True)
    for old in backups[max(1, retain):]:
        old.unlink(missing_ok=True)
        old.with_suffix(old.suffix + ".sha256").unlink(missing_ok=True)
    checkpoint = (
        checkpoint_database(Path(source_path_text), truncate=True)
        if source_path_text
        else None
    )
    return {
        "backup": str(destination),
        "mode": mode,
        "sha256": digest,
        "integrity_check": check,
        "retained": min(len(backups), max(1, retain)),
        "copied": copied,
        "checkpoint": checkpoint,
    }


def verify_backup(source: Path, target: Path | None = None) -> dict:
    if not source.is_file():
        raise ValueError(f"备份不存在：{source}")
    checksum = source.with_suffix(source.suffix + ".sha256")
    expected = checksum.read_text(encoding="utf-8").split()[0] if checksum.exists() else None
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if expected and actual != expected:
        raise RuntimeError("备份 SHA-256 不匹配")
    if target is None:
        temp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        temp.close()
        target = Path(temp.name)
        target.chmod(0o600)
        remove_target = True
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        remove_target = False
    try:
        with gzip.open(source, "rb") as compressed, target.open("wb") as output:
            shutil.copyfileobj(compressed, output)
        target.chmod(0o600)
        db = sqlite3.connect(target)
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        project_count = db.execute(
            "SELECT COUNT(*) FROM projects WHERE scope_type='project'"
        ).fetchone()[0]
        global_scope_count = db.execute(
            "SELECT COUNT(*) FROM projects WHERE scope_type='global'"
        ).fetchone()[0]
        document_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        memory_count = db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
        memory_history_count = db.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0]
        embedding_count = db.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
        db.close()
        if integrity != "ok":
            raise RuntimeError(f"恢复文件完整性检查失败：{integrity}")
        return {
            "source": str(source),
            "target": str(target),
            "mode": "critical" if "knowledge-hub-critical-" in source.name else "full",
            "sha256": actual,
            "integrity_check": integrity,
            "projects": project_count,
            "global_scopes": global_scope_count,
            "documents": document_count,
            "memories": memory_count,
            "memory_history": memory_history_count,
            "memory_embeddings": embedding_count,
        }
    finally:
        if remove_target:
            target.unlink(missing_ok=True)


def load_index_state() -> dict:
    if not INDEX_STATE_FILE.is_file():
        return {"version": 1, "projects": {}}
    try:
        value = json.loads(INDEX_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "projects": {}}
    if not isinstance(value, dict) or not isinstance(value.get("projects"), dict):
        return {"version": 1, "projects": {}}
    return value


def save_index_state(state: dict) -> None:
    INDEX_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    os.replace(temporary, INDEX_STATE_FILE)


def update_git_fingerprint(digest: Any, root: Path) -> bool:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if Path(top).resolve() != root.resolve():
            return False
        digest.update(str(root.resolve()).encode())
        for arguments in (
            ["rev-parse", "HEAD"],
            ["diff", "--no-ext-diff", "--binary", "--", "."],
            ["diff", "--cached", "--no-ext-diff", "--binary", "--", "."],
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ):
            result = subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=True,
                capture_output=True,
                timeout=120,
            )
            digest.update(result.stdout)
            digest.update(b"\0")
            if arguments[0] == "ls-files":
                for raw_path in result.stdout.split(b"\0"):
                    if not raw_path:
                        continue
                    try:
                        stat = (root / os.fsdecode(raw_path)).stat()
                    except OSError:
                        continue
                    digest.update(raw_path)
                    digest.update(f":{stat.st_size}:{stat.st_mtime_ns}".encode())
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def update_git_collection_fingerprint(digest: Any, root: Path) -> bool:
    """Fingerprint a directory whose immediate tree contains Git repositories."""
    found = False
    for current_text, dirs, files in os.walk(root):
        current = Path(current_text)
        if ".git" in dirs or (current / ".git").is_file():
            if not update_git_fingerprint(digest, current):
                return False
            found = True
            dirs[:] = []
            continue
        dirs[:] = [
            name
            for name in dirs
            if name not in kh.SKIP_DIRS and not name.endswith("-backups")
        ]
        # Include files outside nested repositories so collection-level notes
        # and manifests still invalidate the fingerprint.
        for name in sorted(files):
            path = current / name
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(f":{stat.st_size}:{stat.st_mtime_ns}".encode())
    return found


def git_project_fingerprint(project: dict) -> str | None:
    """Return a fingerprint for Git roots or directories containing Git roots."""
    digest = hashlib.sha256()
    for root_text in sorted(project.get("paths", [])):
        root = Path(root_text)
        if update_git_fingerprint(digest, root):
            continue
        if not update_git_collection_fingerprint(digest, root):
            return None
    return digest.hexdigest()


def ingest_all(force: bool = False) -> dict:
    db = kh.connect()
    kh.initialize(db)
    source_path_text = db.execute("PRAGMA database_list").fetchone()[2]
    state = load_index_state()
    results = []
    failures = []
    fingerprint_skipped = []
    now = time.time()
    for project in kh.list_projects(db):
        fingerprint = git_project_fingerprint(project)
        cached = state["projects"].get(project["id"], {})
        last_full = float(cached.get("last_full", 0))
        if (
            not force
            and fingerprint is not None
            and fingerprint == cached.get("fingerprint")
            and now - last_full < FULL_SCAN_INTERVAL_SECONDS
        ):
            fingerprint_skipped.append(project["slug"])
            continue
        try:
            results.append(asdict(kh.ingest_project(db, project["id"])))
            if fingerprint is not None:
                state["projects"][project["id"]] = {
                    "slug": project["slug"],
                    "fingerprint": fingerprint,
                    "last_full": now,
                }
        except Exception as exc:
            failures.append({"project": project["slug"], "error": str(exc)})
    active_ids = {project["id"] for project in kh.list_projects(db)}
    state["projects"] = {
        key: value for key, value in state["projects"].items() if key in active_ids
    }
    save_index_state(state)
    memory = kh.maintain_memories(db)
    embeddings = kh.backfill_memory_embeddings(db)
    db.close()
    checkpoint = (
        checkpoint_database(Path(source_path_text), truncate=False)
        if source_path_text
        else None
    )
    return {
        "projects": len(results),
        "fingerprint_skipped": len(fingerprint_skipped),
        "skipped_projects": fingerprint_skipped,
        "forced": force,
        "failed": failures,
        "indexed": sum(item["indexed"] for item in results),
        "unchanged": sum(item["unchanged"] for item in results),
        "memory": memory,
        "embeddings": embeddings,
        "checkpoint": checkpoint,
    }


def health() -> dict:
    db = kh.connect()
    kh.initialize(db)
    integrity = db.execute("PRAGMA quick_check").fetchone()[0]
    services = {}
    for name, url in {"onyx": "http://127.0.0.1:3000/api/health", "searxng": "http://127.0.0.1:8888/healthz"}.items():
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                services[name] = {"ok": response.status == 200, "status": response.status}
        except Exception as exc:
            services[name] = {"ok": False, "error": str(exc)}
    return {"database": integrity, "services": services, "status": kh.status(db)}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--retain", type=int, default=14)
    backup_parser.add_argument("--mode", choices=("full", "critical"), default="full")
    verify = sub.add_parser("verify-backup")
    verify.add_argument("source", type=Path)
    verify.add_argument("--target", type=Path)
    ingest_parser = sub.add_parser("ingest-all")
    ingest_parser.add_argument("--force", action="store_true")
    checkpoint_parser = sub.add_parser("checkpoint")
    checkpoint_parser.add_argument("--truncate", action="store_true")
    checkpoint_parser.add_argument("--busy-timeout-ms", type=int, default=2_000)
    prune = sub.add_parser("prune-backups")
    prune.add_argument("--full-retain", type=int, default=3)
    prune.add_argument("--critical-retain", type=int, default=14)
    prune.add_argument("--apply", action="store_true")
    memory_review = sub.add_parser("memory-quality-review")
    memory_review.add_argument("--apply", action="store_true")
    sub.add_parser("health")
    sub.add_parser("memory-maintain")
    sub.add_parser("memory-embed")
    args = parser.parse_args()
    handle = lock()
    if args.command == "backup":
        value = backup(args.retain, args.mode)
    elif args.command == "verify-backup":
        value = verify_backup(args.source, args.target)
    elif args.command == "ingest-all":
        value = ingest_all(args.force)
    elif args.command == "checkpoint":
        value = checkpoint_database(
            truncate=args.truncate, busy_timeout_ms=args.busy_timeout_ms
        )
    elif args.command == "prune-backups":
        value = prune_backups(
            args.full_retain, args.critical_retain, args.apply
        )
    elif args.command == "memory-quality-review":
        value = review_automatic_memories(args.apply)
    elif args.command == "memory-maintain":
        db = kh.connect()
        kh.initialize(db)
        value = kh.maintain_memories(db)
        db.close()
    elif args.command == "memory-embed":
        db = kh.connect()
        kh.initialize(db)
        value = kh.backfill_memory_embeddings(db)
        db.close()
    else:
        value = health()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
