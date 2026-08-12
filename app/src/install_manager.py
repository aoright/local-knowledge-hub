#!/usr/bin/env python3
"""Install-time configuration for the portable macOS distribution."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any


MANAGED_BEGIN = "<!-- local-knowledge-hub:begin -->"
MANAGED_END = "<!-- local-knowledge-hub:end -->"
INSTRUCTIONS = """# Shared local knowledge automation

- Use the `local-knowledge` MCP automatically when a task depends on project code, documentation, prior decisions, troubleshooting history, or current external information. The user does not need to name the tool.
- Before substantive project work, call `knowledge_context` with the current workspace absolute path or project name and a concise task-specific query. Keep `include_global=true` so the server combines the current project with a small, relevant global context. Ask which project only if resolution is ambiguous.
- For current or external information, call `web_search` automatically and use `web_fetch` on the most relevant primary sources.
- At task completion, call `knowledge_capture` with `scope=auto` only for an explicit user-authored durable decision, fact, constraint, or runbook. Statements explicitly applying to all projects or globally may enter a global scope; otherwise they remain in the current project. Never capture ordinary chat, guesses, transient debugging, secrets, web claims, or facts already represented by project files.
- When the user corrects, revokes, promotes, or demotes a memory, use `knowledge_update`, `knowledge_forget`, or `knowledge_move` automatically and preserve the user's evidence.
- Keep project retrieval isolated. Search collections only when cross-project scope is explicit; global retrieval is a small read-only supplement, not an all-project search.
"""
LABELS = {
    "start": "com.local-knowledge-hub.start",
    "index": "com.local-knowledge-hub.index",
    "backup": "com.local-knowledge-hub.backup",
}


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def render_template(path: Path, values: dict[str, str]) -> str:
    content = path.read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", value)
    unresolved = re.findall(r"{{[A-Z0-9_]+}}", content)
    if unresolved:
        raise ValueError(f"Unresolved template values in {path.name}: {unresolved}")
    return content


def replace_managed_block(content: str, block: str | None) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(MANAGED_BEGIN)}.*?{re.escape(MANAGED_END)}\n?",
        re.DOTALL,
    )
    content = pattern.sub("\n", content).rstrip()
    if block is None:
        return content + ("\n" if content else "")
    managed = f"{MANAGED_BEGIN}\n{block.rstrip()}\n{MANAGED_END}"
    return f"{content}\n\n{managed}\n" if content else managed + "\n"


def configure_agents(home: Path, enabled: bool) -> list[str]:
    changed: list[str] = []
    for path in (home / ".codex" / "AGENTS.md", home / ".gemini" / "config" / "AGENTS.md"):
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        updated = replace_managed_block(existing, INSTRUCTIONS if enabled else None)
        if updated != existing:
            atomic_write(path, updated)
            changed.append(str(path))
    return changed


def strip_codex_sections(content: str) -> str:
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    skipping = False
    for line in lines:
        section = re.match(r"^\s*\[([^]]+)]\s*$", line)
        if section:
            name = section.group(1).strip()
            skipping = name == "mcp_servers.local-knowledge" or name.startswith(
                "mcp_servers.local-knowledge."
            )
        if not skipping:
            result.append(line)
    return "".join(result).rstrip() + ("\n" if result else "")


def configure_codex(home: Path, command: Path, enabled: bool) -> bool:
    path = home / ".codex" / "config.toml"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated = strip_codex_sections(existing)
    if enabled:
        escaped = str(command).replace("\\", "\\\\").replace('"', '\\"')
        block = (
            "[mcp_servers.local-knowledge]\n"
            f'command = "{escaped}"\n'
            'args = ["mcp"]\n'
            "startup_timeout_sec = 30\n"
            "enabled = true\n\n"
            "[mcp_servers.local-knowledge.tools.web_search]\n"
            'approval_mode = "approve"\n'
        )
        updated = updated.rstrip() + ("\n\n" if updated.strip() else "") + block
    if updated != existing:
        atomic_write(path, updated)
        return True
    return False


def configure_json_mcp(path: Path, command: Path, enabled: bool) -> bool:
    if not enabled and not path.is_file():
        return False
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Refusing to overwrite invalid JSON: {path}") from exc
    else:
        data = {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"mcpServers must be an object: {path}")
    changed = False
    if enabled:
        desired = {"command": str(command), "args": ["mcp"]}
        if servers.get("local-knowledge") != desired:
            servers["local-knowledge"] = desired
            changed = True
    else:
        current = servers.get("local-knowledge")
        if isinstance(current, dict) and current.get("command") == str(command):
            del servers["local-knowledge"]
            changed = True
    if changed or not path.exists():
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return changed


def configure_clients(home: Path, install_root: Path, enabled: bool) -> dict[str, Any]:
    command = install_root / "bin" / "khub"
    return {
        "codex": configure_codex(home, command, enabled),
        "antigravity": configure_json_mcp(
            home / ".gemini" / "config" / "mcp_config.json", command, enabled
        ),
        "antigravity_ide": configure_json_mcp(
            home / ".gemini" / "antigravity-ide" / "mcp_config.json", command, enabled
        ),
        "agents": configure_agents(home, enabled),
    }


def shell_wrapper(python: Path, script: Path, data_root: Path, arguments: str = '"$@"') -> str:
    return (
        "#!/bin/sh\nset -eu\n"
        f"export KHUB_DATA_DIR={json.dumps(str(data_root))}\n"
        f"exec {json.dumps(str(python))} {json.dumps(str(script))} {arguments}\n"
    )


def write_wrappers(install_root: Path) -> None:
    app = install_root / "app"
    data = install_root / "data"
    python = install_root / "venv" / "bin" / "python3"
    wrappers = {
        "khub": shell_wrapper(python, app / "src" / "knowledge_hub.py", data),
        "knowledge-hub-services": shell_wrapper(
            python, app / "src" / "start_services.py", data
        ),
        "knowledge-hub-doctor": shell_wrapper(
            python,
            app / "src" / "install_manager.py",
            data,
            f"doctor --install-root {json.dumps(str(install_root))}",
        ),
    }
    for name, content in wrappers.items():
        atomic_write(install_root / "bin" / name, content, 0o700)


def plist_payload(
    label: str,
    arguments: list[str],
    install_root: Path,
    schedule: dict[str, Any],
    keep_alive: bool = False,
) -> dict[str, Any]:
    data = install_root / "data"
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(install_root / "app"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "KHUB_DATA_DIR": str(data),
        },
        "StandardOutPath": str(data / "logs" / f"{label.rsplit('.', 1)[-1]}.log"),
        "StandardErrorPath": str(data / "logs" / f"{label.rsplit('.', 1)[-1]}.error.log"),
        **schedule,
    }
    if keep_alive:
        payload["KeepAlive"] = True
        payload["ProcessType"] = "Background"
    return payload


def launchctl(action: str, path_or_label: str) -> None:
    if os.environ.get("KHUB_SKIP_LAUNCHCTL") == "1":
        return
    domain = f"gui/{os.getuid()}"
    command = (
        ["launchctl", "bootout", f"{domain}/{path_or_label}"]
        if action == "bootout"
        else ["launchctl", "bootstrap", domain, path_or_label]
    )
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install_launch_agents(home: Path, install_root: Path, services: bool) -> list[str]:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    python = str(install_root / "venv" / "bin" / "python3")
    app = install_root / "app" / "src"
    definitions = {
        "index": plist_payload(
            LABELS["index"], [python, str(app / "maintenance.py"), "ingest-all"],
            install_root, {"StartInterval": 1800},
        ),
        "backup": plist_payload(
            LABELS["backup"],
            [python, str(app / "maintenance.py"), "backup", "--retain", "14"],
            install_root, {"StartCalendarInterval": {"Hour": 3, "Minute": 15}},
        ),
    }
    if services:
        definitions["start"] = plist_payload(
            LABELS["start"], [python, str(app / "start_services.py")], install_root,
            {"RunAtLoad": True}, keep_alive=True,
        )
    installed: list[str] = []
    for suffix, payload in definitions.items():
        path = agents / f"{LABELS[suffix]}.plist"
        launchctl("bootout", LABELS[suffix])
        path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
        path.chmod(0o600)
        launchctl("bootstrap", str(path))
        installed.append(str(path))
    return installed


def uninstall_launch_agents(home: Path) -> list[str]:
    removed: list[str] = []
    for label in LABELS.values():
        path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        launchctl("bootout", label)
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    return removed


def initialize(install_root: Path, source_app: Path, services: bool) -> dict[str, Any]:
    install_root = install_root.expanduser().resolve()
    source_app = source_app.resolve()
    data = install_root / "data"
    config = data / "config"
    for path in (data, config, data / "logs", data / "backups", data / "models"):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)

    values = {
        "USER_AUTH_SECRET": secrets.token_urlsafe(48),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "OPENSEARCH_ADMIN_PASSWORD": "Kh!" + secrets.token_urlsafe(24),
        "SEARXNG_SECRET_KEY": secrets.token_hex(32),
    }
    onyx_env = config / "onyx.env"
    if not onyx_env.is_file():
        atomic_write(
            onyx_env,
            render_template(source_app / "deploy" / "templates" / "onyx.env.template", values),
        )
    searx = config / "searxng" / "settings.yml"
    if not searx.is_file():
        atomic_write(
            searx,
            render_template(
                source_app / "deploy" / "templates" / "searxng-settings.yml.template",
                values,
            ),
        )
    credentials = config / "admin.env"
    if not credentials.is_file():
        atomic_write(
            credentials,
            "# Create the first Onyx account with these local-only credentials.\n"
            "ONYX_ADMIN_EMAIL=admin@local.invalid\n"
            f"ONYX_ADMIN_PASSWORD={secrets.token_urlsafe(24)}\n",
        )

    write_wrappers(install_root)
    home = Path.home()
    clients = configure_clients(home, install_root, True)
    agents = install_launch_agents(home, install_root, services)
    return {
        "install_root": str(install_root),
        "data_root": str(data),
        "clients": clients,
        "launch_agents": agents,
        "onyx_credentials": str(credentials),
    }


def doctor(install_root: Path) -> dict[str, Any]:
    install_root = install_root.expanduser().resolve()
    data = install_root / "data"
    db_path = data / "knowledge-hub.sqlite3"
    checks: dict[str, Any] = {
        "app": (install_root / "app" / "src" / "knowledge_hub.py").is_file(),
        "python": (install_root / "venv" / "bin" / "python3").is_file(),
        "gateway": (install_root / "bin" / "khub").is_file(),
        "private_onyx_config": (data / "config" / "onyx.env").is_file(),
        "private_search_config": (data / "config" / "searxng" / "settings.yml").is_file(),
    }
    if db_path.is_file():
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        checks["database"] = db.execute("PRAGMA quick_check").fetchone()[0]
        checks["projects"] = db.execute(
            "SELECT COUNT(*) FROM projects WHERE scope_type='project'"
        ).fetchone()[0]
        checks["documents"] = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        db.close()
    else:
        checks["database"] = "not_initialized"
    checks["passed"] = all(
        value is True for key, value in checks.items()
        if key in {"app", "python", "gateway", "private_onyx_config", "private_search_config"}
    ) and checks["database"] in {"ok", "not_initialized"}
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("initialize")
    setup.add_argument("--install-root", type=Path, required=True)
    setup.add_argument("--source-app", type=Path, required=True)
    setup.add_argument("--without-services", action="store_true")
    unconfigure = sub.add_parser("unconfigure")
    unconfigure.add_argument("--install-root", type=Path, required=True)
    check = sub.add_parser("doctor")
    check.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "initialize":
        value = initialize(args.install_root, args.source_app, not args.without_services)
    elif args.command == "unconfigure":
        value = {
            "clients": configure_clients(Path.home(), args.install_root.resolve(), False),
            "launch_agents": uninstall_launch_agents(Path.home()),
        }
    else:
        value = doctor(args.install_root)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
