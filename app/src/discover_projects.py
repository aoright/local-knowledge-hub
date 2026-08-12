#!/usr/bin/env python3
"""Discover canonical projects from Codex and Antigravity configuration only."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import uuid
from pathlib import Path

import knowledge_hub as kh


def slugify(value: str) -> str:
    value = urllib.parse.unquote(value).strip().lower().replace("/", "-")
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^\w.\-\u3400-\u9fff]+", "-", value, flags=re.UNICODE)
    return value.strip("-.") or "project"


def unique_slug(db, base: str, table: str = "projects") -> str:
    candidate = slugify(base)
    index = 2
    while db.execute(f"SELECT 1 FROM {table} WHERE slug=?", (candidate,)).fetchone():
        candidate = f"{slugify(base)}-{index}"
        index += 1
    return candidate


def unique_collection_slug(db, base: str, existing_id: str | None = None) -> str:
    candidate = slugify(base)
    if db.execute("SELECT 1 FROM projects WHERE slug=?", (candidate,)).fetchone() or db.execute("SELECT 1 FROM project_aliases WHERE alias=?", (candidate,)).fetchone():
        candidate += "-all"
    stem = candidate
    index = 2
    while db.execute("SELECT 1 FROM project_collections WHERE slug=? AND id<>?", (candidate, existing_id or "")).fetchone():
        candidate = f"{stem}-{index}"
        index += 1
    return candidate


def file_uri_path_text(uri: str, windows: bool | None = None) -> str | None:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return None
    windows = os.name == "nt" if windows is None else windows
    value = urllib.parse.unquote(parsed.path)
    if windows:
        if parsed.netloc:
            value = f"//{parsed.netloc}{value}"
        elif re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    return value


def file_uri_to_path(uri: str) -> Path | None:
    value = file_uri_path_text(uri)
    if value is None:
        return None
    path = Path(value).resolve()
    return path if path.is_dir() else None


def antigravity_candidates() -> list[dict]:
    directory = Path.home() / ".gemini" / "config" / "projects"
    result = []
    for config in sorted(directory.glob("*.json")):
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        paths = []
        for resource in data.get("projectResources", {}).get("resources", []):
            uri = resource.get("folderUri") or resource.get("gitFolder", {}).get("folderUri")
            if uri:
                path = file_uri_to_path(uri)
                if path and str(path) not in paths:
                    paths.append(str(path))
        if paths:
            result.append({"id": data.get("id") or config.stem, "name": urllib.parse.unquote(data.get("name") or config.stem), "paths": paths})
    return result


def codex_paths() -> list[str]:
    config = Path.home() / ".codex" / "config.toml"
    if not config.exists():
        return []
    paths = []
    for value in re.findall(r'^\[projects\."(.*)"\]$', config.read_text(encoding="utf-8"), re.MULTILINE):
        path = Path(value).expanduser().resolve()
        if path.is_dir():
            paths.append(str(path))
    return paths


def owner_for_path(db, path: str) -> str | None:
    row = db.execute("SELECT project_id FROM project_paths WHERE path=? AND active=1", (path,)).fetchone()
    return row["project_id"] if row else None


def add_path(db, project_id: str, path: str) -> None:
    db.execute(
        "INSERT INTO project_paths(project_id,path,active,added_at) VALUES(?,?,1,?) ON CONFLICT(project_id,path) DO UPDATE SET active=1",
        (project_id, path, kh.utcnow()),
    )


def add_alias(db, name: str, project_id: str) -> None:
    alias = slugify(name)
    conflict = db.execute("SELECT id FROM projects WHERE slug=?", (alias,)).fetchone()
    if not conflict:
        db.execute("INSERT OR IGNORE INTO project_aliases(alias,project_id) VALUES(?,?)", (alias, project_id))


def create_collection(db, candidate: dict, owners: set[str]) -> None:
    collection_id = candidate["id"] if re.fullmatch(r"[0-9a-fA-F-]{36}", candidate["id"]) else str(uuid.uuid4())
    existing = db.execute("SELECT id FROM project_collections WHERE id=?", (collection_id,)).fetchone()
    slug = unique_collection_slug(db, candidate["name"], existing["id"] if existing else None)
    now = kh.utcnow()
    db.execute(
        "INSERT INTO project_collections(id,slug,display_name,created_at,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,display_name=excluded.display_name,updated_at=excluded.updated_at",
        (collection_id, slug, candidate["name"], now, now),
    )
    row = db.execute("SELECT id FROM project_collections WHERE id=? OR slug=?", (collection_id, slug)).fetchone()
    for owner in owners:
        db.execute("INSERT OR IGNORE INTO collection_members(collection_id,project_id) VALUES(?,?)", (row["id"], owner))


def main() -> int:
    db = kh.connect()
    kh.initialize(db)
    candidates = sorted(antigravity_candidates(), key=lambda item: (len(item["paths"]), item["name"]))
    for candidate in candidates:
        owners = {owner_for_path(db, path) for path in candidate["paths"]}
        owners.discard(None)
        if not owners:
            slug = unique_slug(db, candidate["name"])
            project_id = candidate["id"] if re.fullmatch(r"[0-9a-fA-F-]{36}", candidate["id"]) else str(uuid.uuid4())
            now = kh.utcnow()
            db.execute(
                "INSERT INTO projects(id,slug,display_name,created_at,updated_at) VALUES(?,?,?,?,?)",
                (project_id, slug, candidate["name"], now, now),
            )
            for path in candidate["paths"]:
                add_path(db, project_id, path)
        elif len(owners) == 1:
            owner = next(iter(owners))
            for path in candidate["paths"]:
                existing = owner_for_path(db, path)
                if existing is None:
                    add_path(db, owner, path)
            add_alias(db, candidate["name"], owner)
        else:
            create_collection(db, candidate, owners)
    for path in codex_paths():
        if owner_for_path(db, path):
            continue
        display_name = Path(path).name
        slug = unique_slug(db, display_name)
        kh.add_project(db, slug, display_name, path)
    kh.audit(db, "projects.discover", None, {"antigravity_configs": len(candidates), "codex_paths": len(codex_paths())})
    db.commit()
    value = kh.status(db)
    print(json.dumps({"projects": len(value["projects"]), "collections": len(value["collections"]), "paths": sum(len(p["paths"]) for p in value["projects"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
