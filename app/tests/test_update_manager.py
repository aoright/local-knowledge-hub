import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "src" / "update_manager.py"
SPEC = importlib.util.spec_from_file_location("update_manager", MODULE_PATH)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class Response:
    def __init__(self, content: bytes, url: str):
        self.content = io.BytesIO(content)
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.content.read(size)

    def geturl(self) -> str:
        return self.url


class UpdateManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.version = self.root / "VERSION"
        self.version.write_text("1.2.3\n", encoding="utf-8")
        self.patches = (
            mock.patch.object(updater, "DATA_ROOT", self.data),
            mock.patch.object(updater, "CONFIG_FILE", self.data / "config" / "update.json"),
            mock.patch.object(updater, "STATE_FILE", self.data / "update-state.json"),
            mock.patch.object(updater, "DOWNLOAD_ROOT", self.data / "updates"),
            mock.patch.object(updater, "LOCK_FILE", self.data / "update.lock"),
            mock.patch.object(updater, "VERSION_CANDIDATES", (self.version,)),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    @staticmethod
    def release(content: bytes = b"signed update") -> dict:
        digest = hashlib.sha256(content).hexdigest()
        return {
            "tag_name": "v1.3.0",
            "draft": False,
            "prerelease": False,
            "html_url": "https://github.com/aoright/local-knowledge-hub/releases/tag/v1.3.0",
            "assets": [
                {
                    "name": "LocalKnowledgeHub-Setup-1.3.0.exe",
                    "browser_download_url": "https://github.com/aoright/local-knowledge-hub/releases/download/v1.3.0/LocalKnowledgeHub-Setup-1.3.0.exe",
                    "size": len(content),
                    "digest": f"sha256:{digest}",
                },
                {
                    "name": "local-knowledge-hub-macos-1.3.0.tar.gz",
                    "browser_download_url": "https://github.com/aoright/local-knowledge-hub/releases/download/v1.3.0/local-knowledge-hub-macos-1.3.0.tar.gz",
                    "size": len(content),
                    "digest": f"sha256:{digest}",
                },
            ],
        }

    def test_default_is_enabled_and_disabled_auto_never_uses_network(self):
        self.assertTrue(updater.status()["auto_update"])
        updater.set_auto_update(False)
        with mock.patch.object(updater, "latest_release") as latest:
            result = updater.check_update(automatic=True)
        self.assertEqual(result["status"], "disabled")
        latest.assert_not_called()

    def test_check_selects_platform_asset_and_api_digest(self):
        with (
            mock.patch.object(updater, "latest_release", return_value=self.release()),
            mock.patch.object(updater, "current_platform", return_value="windows"),
        ):
            result = updater.check_update()
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "1.3.0")
        self.assertEqual(result["asset"]["platform"], "windows")
        self.assertEqual(
            result["asset"]["sha256"], hashlib.sha256(b"signed update").hexdigest()
        )

    def test_download_requires_exact_size_and_digest(self):
        content = b"verified release asset"
        info = {
            "update_available": True,
            "asset": {
                "name": "asset.bin",
                "url": "https://github.com/aoright/local-knowledge-hub/releases/download/v1/asset.bin",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        }
        with mock.patch.object(
            updater.urllib.request,
            "urlopen",
            return_value=Response(content, info["asset"]["url"]),
        ):
            path = updater.download_update(info)
        self.assertEqual(path.read_bytes(), content)
        info["asset"]["sha256"] = "0" * 64
        with mock.patch.object(
            updater.urllib.request,
            "urlopen",
            return_value=Response(content, info["asset"]["url"]),
        ):
            with self.assertRaises(updater.UpdateError):
                updater.download_update(info)

    def test_checksum_asset_is_used_when_api_digest_is_missing(self):
        release = self.release()
        target = release["assets"][0]
        target["digest"] = None
        checksum = hashlib.sha256(b"signed update").hexdigest()
        release["assets"].append({
            "name": target["name"] + ".sha256",
            "browser_download_url": target["browser_download_url"] + ".sha256",
            "size": 100,
        })
        body = f"{checksum}  {target['name']}\n".encode()
        with (
            mock.patch.object(updater, "latest_release", return_value=release),
            mock.patch.object(updater, "current_platform", return_value="windows"),
            mock.patch.object(
                updater.urllib.request,
                "urlopen",
                return_value=Response(body, target["browser_download_url"] + ".sha256"),
            ),
        ):
            result = updater.check_update()
        self.assertEqual(result["asset"]["sha256"], checksum)

    def test_invalid_checksum_encoding_is_reported_as_update_error(self):
        release = self.release()
        target = release["assets"][0]
        release["assets"].append({
            "name": target["name"] + ".sha256",
            "browser_download_url": target["browser_download_url"] + ".sha256",
        })
        with mock.patch.object(
            updater.urllib.request,
            "urlopen",
            return_value=Response(b"\xff", target["browser_download_url"] + ".sha256"),
        ):
            with self.assertRaises(updater.UpdateError):
                updater.checksum_digest(release, target["name"])

    def test_invalid_release_asset_size_is_reported_as_update_error(self):
        release = self.release()
        release["assets"][0]["size"] = "not-a-number"
        with (
            mock.patch.object(updater, "latest_release", return_value=release),
            mock.patch.object(updater, "current_platform", return_value="windows"),
        ):
            with self.assertRaises(updater.UpdateError):
                updater.check_update()

    def test_safe_extract_rejects_parent_traversal(self):
        archive = self.root / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("../escape.txt")
            payload = b"escape"
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
        destination = self.root / "extract"
        destination.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.safe_extract_tar(archive, destination)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_safe_extract_reports_a_corrupt_archive_as_update_error(self):
        archive = self.root / "corrupt.tar.gz"
        archive.write_bytes(b"not a tar archive")
        destination = self.root / "extract-corrupt"
        destination.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.safe_extract_tar(archive, destination)

    def test_windows_installer_preserves_services_and_update_choice(self):
        updater.atomic_json(
            updater.CONFIG_FILE,
            {
                "auto_update": False,
                "services_enabled": False,
                "repository": updater.DEFAULT_REPOSITORY,
            },
        )
        info = {
            "current_version": "1.2.3",
            "latest_version": "1.3.0",
            "asset": {"platform": "windows"},
        }
        setup = self.root / "setup.exe"
        setup.touch()
        with mock.patch.object(updater.subprocess, "run") as runner:
            result = updater.install_update(info, setup, self.root / "install")
        command = runner.call_args.args[0]
        self.assertIn("/COMPONENTS=core", command)
        self.assertIn("/TASKS=!autoupdate", command)
        self.assertEqual(result["installed_version"], "1.3.0")


if __name__ == "__main__":
    unittest.main()
