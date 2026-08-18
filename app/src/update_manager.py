#!/usr/bin/env python3
"""Secure GitHub Release updater for Local Knowledge Hub."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


APP_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = APP_ROOT.parent if APP_ROOT.name == "app" else APP_ROOT
DEFAULT_DATA_ROOT = INSTALL_ROOT / "data" if APP_ROOT.name == "app" else APP_ROOT / "runtime"
DATA_ROOT = Path(
    os.environ.get("KHUB_DATA_DIR", DEFAULT_DATA_ROOT)
).expanduser().resolve()
CONFIG_FILE = DATA_ROOT / "config" / "update.json"
STATE_FILE = DATA_ROOT / "update-state.json"
DOWNLOAD_ROOT = DATA_ROOT / "updates"
LOCK_FILE = DATA_ROOT / "update.lock"
VERSION_CANDIDATES = (
    APP_ROOT / "VERSION",
    INSTALL_ROOT / "VERSION",
    APP_ROOT / "packaging" / "VERSION",
    INSTALL_ROOT / "packaging" / "VERSION",
)
DEFAULT_REPOSITORY = "aoright/local-knowledge-hub"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 16 * 1024
MAX_ASSET_BYTES = 1024 * 1024 * 1024
USER_AGENT = "LocalKnowledgeHub-Updater"
TRUSTED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_platform() -> str:
    override = os.environ.get("KHUB_PLATFORM", "").strip().lower()
    if override in {"macos", "windows"}:
        return override
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise UpdateError("Automatic updates support Windows and macOS only")


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        raise UpdateError(f"Unsupported version: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def current_version() -> str:
    for path in VERSION_CANDIDATES:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            try:
                parse_version(value)
            except UpdateError:
                continue
            return value
    raise UpdateError("Application VERSION file is missing")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Invalid update state: {path}") from exc
    if not isinstance(value, dict):
        raise UpdateError(f"Update state must be an object: {path}")
    return value


def validate_repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise UpdateError("Invalid GitHub repository name")
    return value


def load_settings() -> dict[str, Any]:
    stored = load_json(CONFIG_FILE)
    return {
        **stored,
        "auto_update": bool(stored.get("auto_update", True)),
        "services_enabled": bool(stored.get("services_enabled", True)),
        "repository": validate_repository(
            str(stored.get("repository", DEFAULT_REPOSITORY))
        ),
    }


def set_auto_update(enabled: bool) -> dict[str, Any]:
    settings = load_settings()
    settings["auto_update"] = enabled
    settings["updated_at"] = utcnow()
    atomic_json(CONFIG_FILE, settings)
    return status()


def release_api_url(repository: str) -> str:
    override = os.environ.get("KHUB_UPDATE_API_URL", "").strip()
    if override:
        return override
    return f"https://api.github.com/repos/{repository}/releases/latest"


def validate_https_url(url: str, *, api: bool = False) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise UpdateError("Update URLs must use HTTPS")
    hostname = parsed.hostname.casefold()
    trusted = hostname == "api.github.com" if api else (
        hostname in TRUSTED_DOWNLOAD_HOSTS
        or hostname.endswith(".githubusercontent.com")
    )
    if not trusted and os.environ.get("KHUB_ALLOW_TEST_UPDATE_URLS") != "1":
        raise UpdateError(f"Untrusted update host: {hostname}")


def request(url: str, *, api: bool, maximum: int) -> bytes:
    validate_https_url(url, api=api)
    headers = {
        "Accept": "application/vnd.github+json" if api else "application/octet-stream",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=30
        ) as response:
            final_url = response.geturl()
            validate_https_url(final_url, api=api)
            content = response.read(maximum + 1)
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Could not retrieve update metadata: {exc}") from exc
    if len(content) > maximum:
        raise UpdateError("Update response exceeded the safety limit")
    return content


def latest_release(repository: str) -> dict[str, Any]:
    url = release_api_url(repository)
    try:
        value = json.loads(request(url, api=True, maximum=MAX_METADATA_BYTES))
    except json.JSONDecodeError as exc:
        raise UpdateError("GitHub returned invalid release metadata") from exc
    if not isinstance(value, dict) or not isinstance(value.get("assets"), list):
        raise UpdateError("GitHub release metadata is incomplete")
    if value.get("draft") or value.get("prerelease"):
        raise UpdateError("The latest endpoint returned a non-stable release")
    return value


def asset_name(version: str, platform_name: str) -> str:
    if platform_name == "windows":
        return f"LocalKnowledgeHub-Setup-{version}.exe"
    return f"local-knowledge-hub-macos-{version}.tar.gz"


def find_asset(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    for value in release.get("assets", []):
        if isinstance(value, dict) and value.get("name") == name:
            return value
    return None


def normalize_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", value.strip())
    return match.group(1).lower() if match else None


def checksum_digest(release: dict[str, Any], name: str) -> str:
    checksum = find_asset(release, name + ".sha256")
    if checksum is None or not isinstance(checksum.get("browser_download_url"), str):
        raise UpdateError(f"Release is missing a trusted digest for {name}")
    try:
        content = request(
            checksum["browser_download_url"], api=False, maximum=MAX_CHECKSUM_BYTES
        ).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise UpdateError("Release checksum file is not valid UTF-8") from exc
    for line in content.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            continue
        if len(parts) == 1 or parts[1].lstrip("* ") == name:
            return parts[0].lower()
    raise UpdateError(f"Checksum file does not identify {name}")


def check_update(*, automatic: bool = False) -> dict[str, Any]:
    settings = load_settings()
    installed = current_version()
    if automatic and not settings["auto_update"]:
        return {
            "status": "disabled",
            "auto_update": False,
            "current_version": installed,
        }
    release = latest_release(settings["repository"])
    latest = str(release.get("tag_name", "")).removeprefix("v")
    parse_version(latest)
    available = parse_version(latest) > parse_version(installed)
    result: dict[str, Any] = {
        "status": "update_available" if available else "up_to_date",
        "auto_update": settings["auto_update"],
        "current_version": installed,
        "latest_version": latest,
        "release_url": release.get("html_url"),
        "checked_at": utcnow(),
        "update_available": available,
    }
    if available:
        platform_name = current_platform()
        name = asset_name(latest, platform_name)
        asset = find_asset(release, name)
        if asset is None:
            raise UpdateError(f"Release {latest} is missing {name}")
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            raise UpdateError("Release asset has no download URL")
        validate_https_url(url)
        digest = normalize_digest(asset.get("digest")) or checksum_digest(
            release, name
        )
        try:
            size = int(asset.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise UpdateError("Release asset size is invalid") from exc
        if size <= 0 or size > MAX_ASSET_BYTES:
            raise UpdateError("Release asset size is outside the safety limit")
        result["asset"] = {
            "name": name,
            "url": url,
            "size": size,
            "sha256": digest,
            "platform": platform_name,
        }
    state = load_json(STATE_FILE)
    state.update({key: value for key, value in result.items() if key != "asset"})
    atomic_json(STATE_FILE, state)
    return result


def download_update(info: dict[str, Any]) -> Path:
    if not info.get("update_available") or not isinstance(info.get("asset"), dict):
        raise UpdateError("No update is available")
    asset = info["asset"]
    name = str(asset["name"])
    expected_size = int(asset["size"])
    expected_digest = str(asset["sha256"])
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    destination = DOWNLOAD_ROOT / name
    partial = destination.with_suffix(destination.suffix + ".part")
    validate_https_url(str(asset["url"]))
    headers = {"Accept": "application/octet-stream", "User-Agent": USER_AGENT}
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(
            urllib.request.Request(str(asset["url"]), headers=headers), timeout=60
        ) as response, partial.open("wb") as output:
            validate_https_url(response.geturl())
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_ASSET_BYTES or total > expected_size:
                    raise UpdateError("Downloaded asset exceeded its declared size")
                output.write(block)
                digest.update(block)
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"Could not download the update: {exc}") from exc
    if total != expected_size:
        partial.unlink(missing_ok=True)
        raise UpdateError("Downloaded asset size does not match GitHub metadata")
    if digest.hexdigest() != expected_digest:
        partial.unlink(missing_ok=True)
        raise UpdateError("Downloaded asset failed SHA-256 verification")
    os.replace(partial, destination)
    return destination


def safe_extract_tar(archive: Path, destination: Path) -> Path:
    destination = destination.resolve()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if not members:
                raise UpdateError("Update archive is empty")
            for member in members:
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise UpdateError("Update archive contains an unsafe path")
                if member.issym() or member.islnk() or member.isdev():
                    raise UpdateError(
                        "Update archive contains an unsupported link or device"
                    )
                target = (destination / member_path).resolve()
                if destination != target and destination not in target.parents:
                    raise UpdateError("Update archive escapes the extraction directory")
            bundle.extractall(destination)
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError(f"Could not extract the update archive: {exc}") from exc
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "install.sh").is_file():
        raise UpdateError("Update archive has an unexpected layout")
    return roots[0]


def install_update(info: dict[str, Any], archive: Path, install_root: Path) -> dict[str, Any]:
    settings = load_settings()
    platform_name = str(info["asset"]["platform"])
    environment = os.environ.copy()
    environment["LOCAL_KNOWLEDGE_HOME"] = str(install_root)
    if platform_name == "windows":
        components = "core,services" if settings["services_enabled"] else "core"
        tasks = "autoupdate" if settings["auto_update"] else "!autoupdate"
        command = [
            str(archive),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/COMPONENTS={components}",
            f"/TASKS={tasks}",
        ]
        subprocess.run(command, check=True, env=environment)
    elif platform_name == "macos":
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="extract-", dir=DOWNLOAD_ROOT) as value:
            package = safe_extract_tar(archive, Path(value))
            command = [
                "/bin/sh",
                str(package / "install.sh"),
                "--install-dir",
                str(install_root),
                "--auto-update" if settings["auto_update"] else "--no-auto-update",
            ]
            if not settings["services_enabled"]:
                command.append("--without-services")
            subprocess.run(command, check=True, env=environment)
    else:
        raise UpdateError(f"Unsupported update platform: {platform_name}")
    state = load_json(STATE_FILE)
    state.update({
        "installed_version": info["latest_version"],
        "installed_at": utcnow(),
        "status": "installed",
        "restart_clients_required": True,
    })
    atomic_json(STATE_FILE, state)
    return {
        "status": "installed",
        "previous_version": info["current_version"],
        "installed_version": info["latest_version"],
        "asset": str(archive),
    }


@contextmanager
def update_lock() -> Iterator[None]:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise UpdateError("Another update is already running") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UpdateError("Another update is already running") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def install_latest(*, automatic: bool = False, download_only: bool = False) -> dict[str, Any]:
    info = check_update(automatic=automatic)
    if info["status"] in {"disabled", "up_to_date"}:
        return info
    archive = download_update(info)
    if download_only:
        return {**info, "status": "downloaded", "download": str(archive)}
    return install_update(info, archive, INSTALL_ROOT)


def status() -> dict[str, Any]:
    settings = load_settings()
    state = load_json(STATE_FILE)
    return {
        "current_version": current_version(),
        "auto_update": settings["auto_update"],
        "services_enabled": settings["services_enabled"],
        "repository": settings["repository"],
        "schedule": "daily",
        "last_check": state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Knowledge Hub updater")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("check")
    sub.add_parser("download")
    sub.add_parser("install")
    sub.add_parser("auto")
    toggle = sub.add_parser("set-auto")
    toggle.add_argument("value", choices=("on", "off"))
    args = parser.parse_args()
    try:
        if args.command == "status":
            result = status()
        elif args.command == "check":
            result = check_update()
        elif args.command == "set-auto":
            result = set_auto_update(args.value == "on")
        else:
            with update_lock():
                result = install_latest(
                    automatic=args.command == "auto",
                    download_only=args.command == "download",
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (UpdateError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
