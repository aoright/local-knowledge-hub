#!/usr/bin/env python3
"""Idempotently keep Docker, Onyx, and SearXNG available."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("KHUB_DATA_DIR", ROOT / "runtime")).expanduser().resolve()
SEARCH_CONFIG = Path(
    os.environ.get("KHUB_SEARXNG_CONFIG_DIR", DATA_ROOT / "config" / "searxng")
).expanduser().resolve()
ONYX_ENV = Path(os.environ.get("KHUB_ONYX_ENV", DATA_ROOT / "config" / "onyx.env")).expanduser().resolve()
COLIMA = Path(os.environ.get("KHUB_COLIMA", shutil.which("colima") or "/opt/homebrew/bin/colima"))


def default_docker() -> Path:
    discovered = shutil.which("docker")
    if discovered:
        return Path(discovered)
    if platform.system() == "Windows":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        return program_files / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
    return Path("/opt/homebrew/bin/docker")


DOCKER = Path(os.environ.get("KHUB_DOCKER", str(default_docker())))
ONYX_HEALTH = "http://127.0.0.1:3000/api/health"
SEARXNG_HEALTH = "http://127.0.0.1:8888/healthz"
if platform.system() == "Darwin":
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def run(command: list[str], timeout: int = 900, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        command,
        check=False,
        timeout=timeout,
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {result.returncode}"
        raise RuntimeError(f"{Path(command[0]).name} failed: {message}")


def healthy(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def colima_running() -> bool:
    if not COLIMA.is_file():
        return False
    return subprocess.run(
        [str(COLIMA), "status"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    ).returncode == 0


def docker_running() -> bool:
    if not DOCKER.is_file():
        return False
    return subprocess.run(
        [str(DOCKER), "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
    ).returncode == 0


def docker_desktop() -> Path | None:
    if platform.system() != "Windows":
        return None
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Docker"
        / "Docker"
        / "Docker Desktop.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Docker"
        / "Docker Desktop.exe",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def wait_for_docker(timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if docker_running():
            return True
        time.sleep(3)
    return False


def ensure_docker_runtime() -> str:
    """Recover Docker, including a stale Colima VM whose daemon is unavailable."""
    if docker_running():
        return "ready"
    system = platform.system()
    if system == "Darwin":
        if not COLIMA.is_file():
            raise RuntimeError("Docker daemon 未运行；请启动 Docker Desktop")
        if colima_running():
            # A VM may report running while dockerd or its forwarded socket is
            # unavailable. Give it a short grace period, then restart once.
            if wait_for_docker(45):
                return "recovered"
            run([str(COLIMA), "restart"], timeout=600)
            action = "restarted"
        else:
            architecture = (
                "aarch64" if platform.machine() in {"arm64", "aarch64"} else "x86_64"
            )
            run(
                [
                    str(COLIMA),
                    "start",
                    "--cpus",
                    os.environ.get("KHUB_COLIMA_CPUS", "4"),
                    "--memory",
                    os.environ.get("KHUB_COLIMA_MEMORY_GB", "10"),
                    "--disk",
                    os.environ.get("KHUB_COLIMA_DISK_GB", "80"),
                    "--arch",
                    architecture,
                    "--vm-type",
                    "vz",
                    "--runtime",
                    "docker",
                ],
                timeout=600,
            )
            action = "started"
        if not wait_for_docker(180):
            raise RuntimeError("Colima 已启动，但 Docker daemon 在 180 秒内仍不可用")
        return action
    if system == "Windows":
        desktop = docker_desktop()
        if desktop is None:
            raise RuntimeError("Docker Desktop 未安装；请先安装 Docker Desktop for Windows")
        subprocess.Popen(
            [str(desktop)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if not wait_for_docker():
            raise RuntimeError("Docker Desktop 启动超时")
        return "started"
    raise RuntimeError("Docker daemon 未运行；请启动 Docker Desktop")


def compose_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["KHUB_SEARXNG_CONFIG_DIR"] = str(
        SEARCH_CONFIG if SEARCH_CONFIG.is_dir() else ROOT / "deploy" / "searxng"
    )
    return environment


def compose_files() -> tuple[Path, Path, Path]:
    return (
        ROOT / "deploy" / "searxng-compose.yml",
        ROOT / "vendor" / "onyx" / "deployment" / "docker_compose" / "docker-compose.yml",
        ROOT / "deploy" / "docker-compose.override.yml",
    )


def wait_for_services(timeout: int = 600) -> dict[str, bool]:
    deadline = time.monotonic() + timeout
    result = {"onyx": False, "searxng": False}
    while time.monotonic() < deadline:
        result = {"onyx": healthy(ONYX_HEALTH), "searxng": healthy(SEARXNG_HEALTH)}
        if all(result.values()):
            return result
        time.sleep(5)
    return result


def ensure_services() -> dict[str, object]:
    if not DOCKER.is_file():
        raise RuntimeError("Docker CLI 不存在；请安装 Docker Desktop，或通过 Homebrew 安装 docker 与 colima")

    initial = {"onyx": healthy(ONYX_HEALTH), "searxng": healthy(SEARXNG_HEALTH)}
    # The service endpoints are the user-visible source of truth.  Avoid a
    # potentially slow `docker info` probe every minute when both services are
    # already healthy; transient daemon CLI timeouts previously triggered
    # unnecessary compose recovery and noisy logs despite HTTP 200 responses.
    if all(initial.values()):
        return {"action": "healthy", "services": initial}

    runtime_action = ensure_docker_runtime()

    onyx_env = ONYX_ENV if ONYX_ENV.is_file() else ROOT / "deploy" / ".env"
    compose_env = compose_environment()
    search_compose, onyx_compose, onyx_override = compose_files()

    run([
        str(DOCKER), "compose", "-p", "knowledge-search",
        "-f", str(search_compose), "up", "-d",
    ], env=compose_env)
    run([
        str(DOCKER), "compose", "-p", "knowledge-hub",
        "--env-file", str(onyx_env),
        "-f", str(onyx_compose),
        "-f", str(onyx_override), "up", "-d",
    ])

    services = wait_for_services()
    if not all(services.values()):
        raise RuntimeError(f"服务健康检查超时：{services}")
    return {"action": "started", "runtime": runtime_action, "services": services}


def stop_services(purge: bool = False) -> dict[str, object]:
    if not DOCKER.is_file() or not docker_running():
        return {"action": "not_running"}
    search_compose, onyx_compose, onyx_override = compose_files()
    suffix = ["down", "--remove-orphans"] + (["--volumes"] if purge else [])
    run(
        [
            str(DOCKER),
            "compose",
            "-p",
            "knowledge-search",
            "-f",
            str(search_compose),
            *suffix,
        ],
        env=compose_environment(),
    )
    onyx_env = ONYX_ENV if ONYX_ENV.is_file() else ROOT / "deploy" / ".env"
    run(
        [
            str(DOCKER),
            "compose",
            "-p",
            "knowledge-hub",
            "--env-file",
            str(onyx_env),
            "-f",
            str(onyx_compose),
            "-f",
            str(onyx_override),
            *suffix,
        ]
    )
    return {"action": "stopped", "purged": purge}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只执行一次，供交互式维护使用")
    parser.add_argument("--stop", action="store_true", help="停止 Onyx 与 SearXNG")
    parser.add_argument("--purge", action="store_true", help="停止服务时同时删除 Docker volumes")
    args = parser.parse_args()

    if args.stop:
        print(json.dumps(stop_services(args.purge), ensure_ascii=False), flush=True)
        return 0

    last_message = ""
    repeated = 0
    retry_delay = 30
    while True:
        try:
            result = ensure_services()
            message = json.dumps(result, ensure_ascii=False, sort_keys=True)
            if message != last_message:
                print(message, flush=True)
                last_message = message
                repeated = 0
            if args.once:
                return 0
            retry_delay = 30
            time.sleep(60)
        except Exception as exc:
            payload = {"action": "error", "error": str(exc)}
            message = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            repeated += 1
            # Emit the first occurrence, every changed error, then one compact
            # reminder every 30 repeats instead of flooding launchd logs.
            if message != last_message or repeated % 30 == 0:
                if repeated > 1:
                    payload["repeated"] = repeated
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
                last_message = message
            if args.once:
                return 1
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)


if __name__ == "__main__":
    raise SystemExit(main())
