#!/usr/bin/env python3
"""Idempotently keep Colima, Onyx, and SearXNG available on macOS."""

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
DOCKER = Path(os.environ.get("KHUB_DOCKER", shutil.which("docker") or "/opt/homebrew/bin/docker"))
ONYX_HEALTH = "http://127.0.0.1:3000/api/health"
SEARXNG_HEALTH = "http://127.0.0.1:8888/healthz"
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def run(command: list[str], timeout: int = 900, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, timeout=timeout, env=env)


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
    if all(initial.values()) and docker_running():
        return {"action": "healthy", "services": initial}

    if not docker_running() and COLIMA.is_file() and not colima_running():
        architecture = "aarch64" if platform.machine() in {"arm64", "aarch64"} else "x86_64"
        run([
            str(COLIMA), "start",
            "--cpus", os.environ.get("KHUB_COLIMA_CPUS", "4"),
            "--memory", os.environ.get("KHUB_COLIMA_MEMORY_GB", "10"),
            "--disk", os.environ.get("KHUB_COLIMA_DISK_GB", "80"),
            "--arch", architecture, "--vm-type", "vz", "--runtime", "docker",
        ], timeout=600)
    elif not docker_running() and not COLIMA.is_file():
        raise RuntimeError("Docker daemon 未运行；请启动 Docker Desktop")

    if not docker_running():
        raise RuntimeError("Docker daemon 启动失败")

    search_config = SEARCH_CONFIG if SEARCH_CONFIG.is_dir() else ROOT / "deploy" / "searxng"
    onyx_env = ONYX_ENV if ONYX_ENV.is_file() else ROOT / "deploy" / ".env"
    compose_env = os.environ.copy()
    compose_env["KHUB_SEARXNG_CONFIG_DIR"] = str(search_config)

    run([
        str(DOCKER), "compose", "-p", "knowledge-search",
        "-f", str(ROOT / "deploy/searxng-compose.yml"), "up", "-d",
    ], env=compose_env)
    run([
        str(DOCKER), "compose", "-p", "knowledge-hub",
        "--env-file", str(onyx_env),
        "-f", str(ROOT / "vendor/onyx/deployment/docker_compose/docker-compose.yml"),
        "-f", str(ROOT / "deploy/docker-compose.override.yml"), "up", "-d",
    ])

    services = wait_for_services()
    if not all(services.values()):
        raise RuntimeError(f"服务健康检查超时：{services}")
    return {"action": "started", "services": services}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只执行一次，供交互式维护使用")
    args = parser.parse_args()

    while True:
        try:
            result = ensure_services()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return 0
            time.sleep(60)
        except Exception as exc:
            print(json.dumps({"action": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
            if args.once:
                return 1
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
