#!/usr/bin/env python3
"""Install-time configuration for the portable macOS and Windows distributions."""

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

- For every task that may read, explain, diagnose, edit, test, review, or build project files, make `knowledge_context` the first tool call before planning or inspecting files. Do this once per task even when the task looks simple or the user did not mention local knowledge.
- `workspace_path` is mandatory for `knowledge_context`: always pass the current IDE workspace or current file absolute path and a concise task-specific query; never call it with only `query`. Add `project` only to override workspace resolution. Keep `include_global=true` so the server combines the current project with a small, relevant global context. Ask which project only if resolution is ambiguous. Do not call it for ordinary conversation unrelated to project work.
- For current or external information, call `web_search` automatically and use `web_fetch` on the most relevant primary sources.
- Before the final response, proactively review the current user's own messages; do not wait for the user to say “remember this.” If and only if the user explicitly authored a durable decision, fact, constraint, or runbook, call `knowledge_capture` automatically with `scope=auto` and preserve the user's statement as evidence. A task request, UI tweak, question, transient defect, assistant implementation result, or fact already represented by project files is not a memory. Determine global scope only from the user's evidence: it must explicitly say all projects, cross-project, or global policy; never infer scope from an assistant-generated title or summary. Otherwise keep it in the current project. Never capture ordinary chat, guesses, transient debugging, secrets, or web claims.
- When the user corrects, revokes, promotes, or demotes a memory, use `knowledge_update`, `knowledge_forget`, or `knowledge_move` automatically and preserve the user's evidence.
- Keep project retrieval isolated. Search collections only when cross-project scope is explicit; global retrieval is a small read-only supplement, not an all-project search.
"""
LABELS = {
    "start": "com.local-knowledge-hub.start",
    "index": "com.local-knowledge-hub.index",
    "backup": "com.local-knowledge-hub.backup",
}
WINDOWS_TASKS = {
    "services": "LocalKnowledgeHub-Services",
    "index": "LocalKnowledgeHub-Index",
    "backup": "LocalKnowledgeHub-Backup",
}


def current_platform() -> str:
    override = os.environ.get("KHUB_PLATFORM", "").strip().lower()
    if override in {"macos", "windows"}:
        return override
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "other"


def runtime_python(install_root: Path, platform_name: str | None = None) -> Path:
    platform_name = platform_name or current_platform()
    if platform_name == "windows":
        return install_root / "venv" / "Scripts" / "python.exe"
    return install_root / "venv" / "bin" / "python3"


def gateway_path(install_root: Path, platform_name: str | None = None) -> Path:
    platform_name = platform_name or current_platform()
    suffix = ".cmd" if platform_name == "windows" else ""
    return install_root / "bin" / f"khub{suffix}"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


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


def remove_legacy_instruction_section(content: str) -> str:
    """Remove the exact pre-managed local-knowledge section, preserving peers."""
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    skipping_level: int | None = None
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+Shared Local Knowledge Automation\s*$", line, re.I)
        if heading:
            skipping_level = len(heading.group(1))
            continue
        if skipping_level is not None:
            next_heading = re.match(r"^(#{1,6})\s+", line)
            if not next_heading or len(next_heading.group(1)) > skipping_level:
                continue
            skipping_level = None
        output.append(line)
    return "".join(output).rstrip() + ("\n" if output else "")


def configure_agents(home: Path, enabled: bool) -> list[str]:
    changed: list[str] = []
    # Codex and Antigravity use different official global-rule locations.
    for path in (home / ".codex" / "AGENTS.md", home / ".gemini" / "GEMINI.md"):
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        migrated = remove_legacy_instruction_section(
            replace_managed_block(existing, None)
        )
        updated = replace_managed_block(migrated, INSTRUCTIONS if enabled else None)
        if updated != existing:
            atomic_write(path, updated)
            changed.append(str(path))
    # Remove only our obsolete managed block from the pre-1.2 location. Any
    # unrelated user content in that file is preserved.
    legacy = home / ".gemini" / "config" / "AGENTS.md"
    if legacy.is_file():
        existing = legacy.read_text(encoding="utf-8")
        updated = remove_legacy_instruction_section(
            replace_managed_block(existing, None)
        )
        if updated != existing:
            atomic_write(legacy, updated)
            changed.append(str(legacy))
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


def configure_codex(
    home: Path,
    command: Path,
    arguments: list[str],
    environment: dict[str, str],
    enabled: bool,
) -> bool:
    path = home / ".codex" / "config.toml"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated = strip_codex_sections(existing)
    if enabled:
        args = ", ".join(toml_string(value) for value in arguments)
        block = (
            "[mcp_servers.local-knowledge]\n"
            f"command = {toml_string(str(command))}\n"
            f"args = [{args}]\n"
            "startup_timeout_sec = 30\n"
            "enabled = true\n\n"
            "[mcp_servers.local-knowledge.env]\n"
            + "".join(
                f"{key} = {toml_string(value)}\n"
                for key, value in sorted(environment.items())
            )
            + "\n"
            "[mcp_servers.local-knowledge.tools.web_search]\n"
            'approval_mode = "approve"\n'
        )
        updated = updated.rstrip() + ("\n\n" if updated.strip() else "") + block
    if updated != existing:
        atomic_write(path, updated)
        return True
    return False


def configure_json_mcp(
    path: Path,
    command: Path,
    arguments: list[str],
    environment: dict[str, str],
    enabled: bool,
) -> bool:
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
        desired = {
            "command": str(command),
            "args": arguments,
            "env": environment,
        }
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
    command = runtime_python(install_root)
    arguments = [str(install_root / "app" / "src" / "knowledge_hub.py"), "mcp"]
    environment = {"KHUB_DATA_DIR": str(install_root / "data")}
    return {
        "codex": configure_codex(home, command, arguments, environment, enabled),
        "antigravity": configure_json_mcp(
            home / ".gemini" / "config" / "mcp_config.json",
            command,
            arguments,
            environment,
            enabled,
        ),
        "antigravity_ide": configure_json_mcp(
            home / ".gemini" / "antigravity-ide" / "mcp_config.json",
            command,
            arguments,
            environment,
            enabled,
        ),
        "agents": configure_agents(home, enabled),
    }


def shell_wrapper(python: Path, script: Path, data_root: Path, arguments: str = '"$@"') -> str:
    return (
        "#!/bin/sh\nset -eu\n"
        f"export KHUB_DATA_DIR={json.dumps(str(data_root))}\n"
        f"exec {json.dumps(str(python))} {json.dumps(str(script))} {arguments}\n"
    )


def batch_value(value: str) -> str:
    return value.replace("%", "%%")


def batch_wrapper(
    python: Path,
    script: Path,
    data_root: Path,
    fixed_arguments: list[str] | None = None,
    forward_arguments: bool = False,
) -> str:
    values = [str(python), str(script), *(fixed_arguments or [])]
    command = " ".join(f'"{batch_value(value)}"' for value in values)
    if forward_arguments:
        command += " %*"
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "KHUB_DATA_DIR={batch_value(str(data_root))}"\r\n'
        f"{command}\r\n"
        "exit /b %errorlevel%\r\n"
    )


def write_wrappers(install_root: Path, platform_name: str | None = None) -> None:
    platform_name = platform_name or current_platform()
    app = install_root / "app"
    data = install_root / "data"
    python = runtime_python(install_root, platform_name)
    if platform_name == "windows":
        wrappers = {
            "khub.cmd": batch_wrapper(
                python, app / "src" / "knowledge_hub.py", data, forward_arguments=True
            ),
            "knowledge-hub-services.cmd": batch_wrapper(
                python, app / "src" / "start_services.py", data, ["--once"]
            ),
            "knowledge-hub-index.cmd": batch_wrapper(
                python, app / "src" / "maintenance.py", data, ["ingest-all"]
            ),
            "knowledge-hub-backup.cmd": batch_wrapper(
                python,
                app / "src" / "maintenance.py",
                data,
                ["backup", "--mode", "critical", "--retain", "14"],
            ),
            "knowledge-hub-doctor.cmd": batch_wrapper(
                python,
                app / "src" / "install_manager.py",
                data,
                ["doctor", "--install-root", str(install_root)],
            ),
        }
        for name, content in wrappers.items():
            atomic_write(install_root / "bin" / name, content, 0o700)
        return
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
            [python, str(app / "maintenance.py"), "backup", "--mode", "critical", "--retain", "14"],
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


def schtasks(arguments: list[str], check: bool = True) -> subprocess.CompletedProcess[str] | None:
    if os.environ.get("KHUB_SKIP_SCHEDULER") == "1":
        return None
    return subprocess.run(
        ["schtasks.exe", *arguments],
        check=check,
        text=True,
        capture_output=True,
    )


def scheduled_command(wrapper: Path) -> str:
    return f'cmd.exe /d /c ""{wrapper}""'


def install_scheduled_tasks(install_root: Path, services: bool) -> list[str]:
    definitions = {
        "index": ["/SC", "MINUTE", "/MO", "30"],
        "backup": ["/SC", "DAILY", "/ST", "03:15"],
    }
    if services:
        definitions["services"] = ["/SC", "MINUTE", "/MO", "5"]
    installed: list[str] = []
    for suffix, schedule in definitions.items():
        task_name = WINDOWS_TASKS[suffix]
        wrapper = install_root / "bin" / f"knowledge-hub-{suffix}.cmd"
        schtasks(["/Delete", "/TN", task_name, "/F"], check=False)
        schtasks(
            [
                "/Create",
                "/TN",
                task_name,
                *schedule,
                "/TR",
                scheduled_command(wrapper),
                "/RL",
                "LIMITED",
                "/F",
            ]
        )
        installed.append(task_name)
    if not services:
        schtasks(["/Delete", "/TN", WINDOWS_TASKS["services"], "/F"], check=False)
    return installed


def uninstall_scheduled_tasks() -> list[str]:
    removed: list[str] = []
    for task_name in WINDOWS_TASKS.values():
        result = schtasks(["/Delete", "/TN", task_name, "/F"], check=False)
        if result is None or result.returncode == 0:
            removed.append(task_name)
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

    platform_name = current_platform()
    if platform_name not in {"macos", "windows"}:
        raise RuntimeError(f"Unsupported platform: {platform_name}")
    write_wrappers(install_root, platform_name)
    home = Path.home()
    clients = configure_clients(home, install_root, True)
    agents: list[str] = []
    tasks: list[str] = []
    if platform_name == "windows":
        tasks = install_scheduled_tasks(install_root, services)
    else:
        agents = install_launch_agents(home, install_root, services)
    return {
        "platform": platform_name,
        "install_root": str(install_root),
        "data_root": str(data),
        "clients": clients,
        "launch_agents": agents,
        "scheduled_tasks": tasks,
        "onyx_credentials": str(credentials),
    }


def doctor(install_root: Path) -> dict[str, Any]:
    install_root = install_root.expanduser().resolve()
    data = install_root / "data"
    db_path = data / "knowledge-hub.sqlite3"
    platform_name = current_platform()
    checks: dict[str, Any] = {
        "app": (install_root / "app" / "src" / "knowledge_hub.py").is_file(),
        "python": runtime_python(install_root, platform_name).is_file(),
        "gateway": gateway_path(install_root, platform_name).is_file(),
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
    sub.add_parser("configure-rules")
    args = parser.parse_args()

    if args.command == "initialize":
        value = initialize(args.install_root, args.source_app, not args.without_services)
    elif args.command == "unconfigure":
        platform_name = current_platform()
        value = {
            "clients": configure_clients(Path.home(), args.install_root.resolve(), False),
            "launch_agents": (
                uninstall_launch_agents(Path.home()) if platform_name == "macos" else []
            ),
            "scheduled_tasks": (
                uninstall_scheduled_tasks() if platform_name == "windows" else []
            ),
        }
    elif args.command == "doctor":
        value = doctor(args.install_root)
    else:
        value = {"changed": configure_agents(Path.home(), True)}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
