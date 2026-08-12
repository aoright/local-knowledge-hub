#!/usr/bin/env python3
"""Incrementally ingest every discovered canonical project."""

from __future__ import annotations

import json
from dataclasses import asdict

import knowledge_hub as kh


def main() -> int:
    db = kh.connect()
    kh.initialize(db)
    projects = kh.list_projects(db)
    results = []
    failures = []
    for index, project in enumerate(projects, 1):
        try:
            stats = asdict(kh.ingest_project(db, project["id"]))
            results.append(stats)
            print(json.dumps({"progress": f"{index}/{len(projects)}", **stats}, ensure_ascii=False), flush=True)
        except Exception as exc:
            item = {"project": project["slug"], "error": str(exc)}
            failures.append(item)
            print(json.dumps({"progress": f"{index}/{len(projects)}", **item}, ensure_ascii=False), flush=True)
    print(json.dumps({
        "projects": len(projects),
        "succeeded": len(results),
        "failed": len(failures),
        "indexed": sum(item["indexed"] for item in results),
        "unchanged": sum(item["unchanged"] for item in results),
        "chunks_added": sum(item["chunks"] for item in results),
        "failures": failures,
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
