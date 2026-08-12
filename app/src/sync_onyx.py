#!/usr/bin/env python3
"""Incrementally mirror canonical Knowledge Hub documents into local Onyx."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import knowledge_hub as kh


ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }


def opener_for(base_url: str, email: str, password: str) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"username": email, "password": password}).encode()
    request = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with opener.open(request, timeout=30) as response:
        if response.status != 204:
            raise RuntimeError(f"Onyx 登录失败：HTTP {response.status}")
    return opener


def document_payload(db, row) -> dict:
    project = db.execute("SELECT slug,display_name FROM projects WHERE id=?", (row["project_id"],)).fetchone()
    pieces = [
        chunk["content"]
        for chunk in db.execute(
            "SELECT content FROM chunks WHERE document_id=? ORDER BY chunk_index", (row["id"],)
        )
    ]
    return {
        "document": {
            "id": f"khub:{row['id']}",
            "sections": [{"type": "text", "text": piece, "link": row["source_uri"]} for piece in pieces],
            "source": "ingestion_api",
            "semantic_identifier": row["title"],
            "title": row["title"],
            "metadata": {
                "project_id": row["project_id"],
                "project_slug": project["slug"],
                "project_name": project["display_name"],
                "source_type": row["source_type"],
                "relative_path": row["relative_path"] or "",
                "content_hash": row["content_hash"],
                "trust": "local_project" if row["source_type"] == "file" else "controlled_memory",
            },
            "doc_updated_at": row["modified_at"] or row["indexed_at"],
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--db", type=Path, default=kh.DEFAULT_DB)
    args = parser.parse_args()
    auth_path = Path(os.environ.get("KHUB_ADMIN_ENV", kh.DATA_ROOT / "config" / "admin.env"))
    if not auth_path.is_file():
        auth_path = ROOT / "deploy" / "admin.env"
    auth = load_env(auth_path)
    opener = opener_for(args.base_url, auth["ONYX_ADMIN_EMAIL"], auth["ONYX_ADMIN_PASSWORD"])
    db = kh.connect(args.db)
    kh.initialize(db)
    params: list[object] = []
    where = ""
    if args.project:
        project = kh.get_project(db, args.project)
        where = "WHERE project_id=?"
        params.append(project["id"])
    sql = f"SELECT * FROM documents {where} ORDER BY project_id,source_type,source_key"
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    rows = db.execute(sql, params).fetchall()
    succeeded = 0
    failed: list[dict[str, str]] = []
    for index, row in enumerate(rows, 1):
        payload = document_payload(db, row)
        request = urllib.request.Request(
            f"{args.base_url}/api/onyx-api/ingestion",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=180) as response:
                result = json.load(response)
            succeeded += 1
            print(json.dumps({"progress": f"{index}/{len(rows)}", "title": row["title"], "result": result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            failed.append({"document_id": row["id"], "title": row["title"], "error": str(exc)})
            print(json.dumps({"progress": f"{index}/{len(rows)}", "title": row["title"], "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        time.sleep(0.02)
    summary = {"total": len(rows), "succeeded": succeeded, "failed": len(failed), "failures": failed[:20]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
