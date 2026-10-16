"""Offline regression tests for immutable source downloads and cache recovery."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import download_noto


class Response(io.BytesIO):
    def __init__(self, data: bytes, url: str, disconnect: bool = False):
        super().__init__(data)
        self.url = url
        self.disconnect = disconnect
        self.read_count = 0

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_count += 1
        if self.disconnect:
            if self.read_count > 1:
                raise ConnectionResetError("connection lost during download")
            size = 2
        return super().read(size)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tofu-download-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.manifest_path = self.root / "noto.lock.json"
        self.payloads = {"font": b"OTTO-example-font", "license": b"Example OFL license"}
        self.manifest = {
            "schema_version": 1,
            "revision": "a" * 40,
            "files": {
                role: {
                    "filename": filename,
                    "url": f"https://example.org/{'a' * 40}/{filename}",
                    "sha256": hashlib.sha256(self.payloads[role]).hexdigest(),
                    "size": len(self.payloads[role]),
                }
                for role, filename in (("font", "example.otf"), ("license", "OFL.txt"))
            },
        }
        self.write_manifest(self.manifest)
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def write_manifest(self, manifest):
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def fill_cache(self):
        for role, entry in self.manifest["files"].items():
            (self.cache / entry["filename"]).write_bytes(self.payloads[role])

    def fetch(self, request, **kwargs):
        for role, entry in self.manifest["files"].items():
            if request.full_url == entry["url"]:
                return Response(self.payloads[role], entry["url"])
        self.fail(f"unexpected download: {request.full_url}")

    def test_fresh_download_and_corrupt_cache_repair(self):
        with patch.object(download_noto, "urlopen", side_effect=self.fetch) as network:
            paths = download_noto.download_sources(self.manifest_path, self.cache)
            self.assertEqual(network.call_count, 2)
            for role, path in paths.items():
                self.assertEqual(path.read_bytes(), self.payloads[role])
            # Same length corruption must still be detected by the digest.
            paths["font"].write_bytes(b"x" * len(self.payloads["font"]))
            network.reset_mock()
            download_noto.download_sources(self.manifest_path, self.cache)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(paths["font"].read_bytes(), self.payloads["font"])
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), ["OFL.txt", "example.otf"])

    def test_verified_cache_never_uses_network(self):
        self.fill_cache()
        with patch.object(download_noto, "urlopen", side_effect=AssertionError("network used")) as network:
            for offline in (False, True):
                paths = download_noto.download_sources(self.manifest_path, self.cache, offline=offline)
                self.assertEqual(paths["font"].read_bytes(), self.payloads["font"])
            network.assert_not_called()

    def test_offline_rejects_missing_truncated_and_same_size_corruption(self):
        font = self.cache / "example.otf"
        for invalid in (None, b"short", b"x" * len(self.payloads["font"])):
            with self.subTest(cache_content=invalid):
                self.fill_cache()
                if invalid is None:
                    font.unlink()
                else:
                    font.write_bytes(invalid)
                with patch.object(download_noto, "urlopen") as network:
                    with self.assertRaisesRegex(ValueError, "missing or corrupt cached font"):
                        download_noto.download_sources(self.manifest_path, self.cache, offline=True)
                    network.assert_not_called()

    def test_download_failures_preserve_existing_file_and_remove_partial_files(self):
        entry = self.manifest["files"]["font"]
        destination = self.cache / entry["filename"]
        old = b"previous source must survive"
        cases = (
            (b"x" * entry["size"], False, ValueError),
            (b"x" * (entry["size"] + 1), False, ValueError),
            (self.payloads["font"], True, ConnectionResetError),
        )
        for data, disconnect, expected_error in cases:
            with self.subTest(disconnect=disconnect, size=len(data)):
                destination.write_bytes(old)
                with patch.object(download_noto, "urlopen", return_value=Response(data, entry["url"], disconnect)):
                    with self.assertRaises(expected_error):
                        download_noto.download_file(entry, destination)
                self.assertEqual(destination.read_bytes(), old)
                self.assertEqual(list(self.cache.iterdir()), [destination])

    def test_failed_initial_download_does_not_install_a_file(self):
        entry = self.manifest["files"]["font"]
        destination = self.cache / entry["filename"]
        with patch.object(download_noto, "urlopen", return_value=Response(b"truncated", entry["url"])):
            with self.assertRaises(ValueError):
                download_noto.download_file(entry, destination)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_http_redirect_is_rejected(self):
        entry = self.manifest["files"]["font"]
        with patch.object(download_noto, "urlopen", return_value=Response(self.payloads["font"], "http://example.org/file")):
            with self.assertRaisesRegex(ValueError, "non-HTTPS"):
                download_noto.download_file(entry, self.cache / entry["filename"])
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_manifest_rejects_unsafe_or_unpinned_inputs(self):
        cases = (
            ("filename", "../escape.otf"),
            ("filename", "..\\escape.otf"),
            ("filename", "OFL.txt"),
            ("url", "https://example.org/main/font.otf"),
            ("url", f"http://example.org/{'a' * 40}/font.otf"),
            ("sha256", "not-a-hash"),
            ("size", -1),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                manifest = copy.deepcopy(self.manifest)
                manifest["files"]["font"][key] = value
                self.write_manifest(manifest)
                with patch.object(download_noto, "urlopen") as network:
                    with self.assertRaises(ValueError):
                        download_noto.download_sources(self.manifest_path, self.cache)
                    network.assert_not_called()
                self.assertEqual(list(self.cache.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
