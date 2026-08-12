#!/usr/bin/env python3
"""Backup, restore validation, automatic indexing, and health checks."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import knowledge_hub as kh


ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = kh.DATA_ROOT / "backups"
LOCK_FILE = kh.DATA_ROOT / "maintenance.lock"


def lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.chmod(0o700)
    handle = LOCK_FILE.open("a+")
    LOCK_FILE.chmod(0o600)
    # Scheduled indexing and the daily backup may occasionally overlap. Queue
    # maintenance jobs instead of failing a once-per-day backup immediately.
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def backup(retain: int = 14) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUP_DIR / f"knowledge-hub-{stamp}.sqlite3.gz"
    with tempfile.TemporaryDirectory(dir=BACKUP_DIR) as temp_dir:
        snapshot = Path(temp_dir) / "snapshot.sqlite3"
        source_db = kh.connect()
        target_db = sqlite3.connect(snapshot)
        source_db.backup(target_db)
        target_db.close()
        source_db.close()
        check = sqlite3.connect(snapshot).execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"备份完整性检查失败：{check}")
        with snapshot.open("rb") as source, gzip.open(destination, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target)
    destination.chmod(0o600)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checksum = destination.with_suffix(destination.suffix + ".sha256")
    checksum.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    checksum.chmod(0o600)
    backups = sorted(BACKUP_DIR.glob("knowledge-hub-*.sqlite3.gz"), reverse=True)
    for old in backups[max(1, retain):]:
        old.unlink(missing_ok=True)
        old.with_suffix(old.suffix + ".sha256").unlink(missing_ok=True)
    return {"backup": str(destination), "sha256": digest, "integrity_check": check, "retained": min(len(backups), max(1, retain))}


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


def ingest_all() -> dict:
    db = kh.connect()
    kh.initialize(db)
    results = []
    failures = []
    for project in kh.list_projects(db):
        try:
            results.append(asdict(kh.ingest_project(db, project["id"])))
        except Exception as exc:
            failures.append({"project": project["slug"], "error": str(exc)})
    memory = kh.maintain_memories(db)
    embeddings = kh.backfill_memory_embeddings(db)
    return {"projects": len(results), "failed": failures, "indexed": sum(item["indexed"] for item in results), "unchanged": sum(item["unchanged"] for item in results), "memory": memory, "embeddings": embeddings}


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
    verify = sub.add_parser("verify-backup")
    verify.add_argument("source", type=Path)
    verify.add_argument("--target", type=Path)
    sub.add_parser("ingest-all")
    sub.add_parser("health")
    sub.add_parser("memory-maintain")
    sub.add_parser("memory-embed")
    args = parser.parse_args()
    handle = lock()
    if args.command == "backup":
        value = backup(args.retain)
    elif args.command == "verify-backup":
        value = verify_backup(args.source, args.target)
    elif args.command == "ingest-all":
        value = ingest_all()
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
