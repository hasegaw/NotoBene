"""Release ZIP provenance, reproducibility and corruption regression tests."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts import release


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="noto-release-test-")
        self.addCleanup(temporary.cleanup)
        self.dist = Path(temporary.name)
        self.lock = json.loads((release.ROOT / "sources/noto.lock.json").read_text())
        self.report = {
            "font_sha256": release.file_record(b"test-font")["sha256"],
            "source_sha256": self.lock["files"]["font"]["sha256"],
            "version": (release.ROOT / "sources/font-version.txt").read_text().strip(),
            "source_date_epoch": 1704067200,
        }
        (self.dist / "NotoBene-Regular.ttf").write_bytes(b"test-font")
        (self.dist / "OFL.txt").write_bytes((release.ROOT / "sources/OFL.txt").read_bytes())
        (self.dist / "FONT-NOTICE.txt").write_bytes((release.ROOT / "FONT-NOTICE.txt").read_bytes())
        self.write_json("noto.lock.json", self.lock)
        self.write_json("build-report.json", self.report)
        self.validation = {
            "ok": True, "source_checked": True,
            "font_sha256": self.report["font_sha256"],
            "source_sha256": self.report["source_sha256"],
        }
        self.write_json("validation.json", self.validation)
        (self.dist / "preview.png").write_bytes(b"excluded-image")

    def write_json(self, name, value):
        (self.dist / name).write_bytes(release.json_bytes(value))

    def package(self, date="20261016", revision="1"):
        return release.package(self.dist, date, revision, "example/noto-bene", "b" * 40)

    def rewrite_archive(self, archive, contents):
        with zipfile.ZipFile(archive, "w") as zipped:
            for name, content in contents.items():
                zipped.writestr(name, content)
        digest = release.file_record(archive.read_bytes())["sha256"]
        (self.dist / "release/SHA256SUMS").write_text(f"{digest}  {archive.name}\n")

    def test_names_sort_by_date_and_revision(self):
        identity = lambda date, rev: release.release_identity(date, rev, "a" * 40)[1]
        names = [identity("20261101", 1), identity("20261016", 10), identity("20261016", 2)]
        self.assertEqual(sorted(names), [names[2], names[1], names[0]])
        self.assertEqual(names[2], "NotoBene-20261016-noto-aaaaaaaaaaaa-r02.zip")

    def test_invalid_dates_revisions_and_sources_are_rejected(self):
        for date in ("20260230", "2026101", "2026-10-16", "19790101", "21080101"):
            with self.subTest(date=date), self.assertRaises(ValueError):
                self.package(date=date)
        for revision in (0, 100, -1, True, "1.5", "1;bad", "001"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.package(revision=revision)
        with self.assertRaises(ValueError):
            release.release_identity("20261016", 1, "main")
        for repository, sha in (("../repo", "a" * 40), ("owner/repo", "HEAD")):
            with self.assertRaises(ValueError):
                release.validate_origin(repository, sha)

    def test_archive_reproducible_despite_mtime_permissions_and_extra_files(self):
        manifest = self.package()
        archive = self.dist / "release" / manifest["archive"]
        first = archive.read_bytes()
        for name in release.PAYLOAD_FILES:
            path = self.dist / name
            os.utime(path, (1800000000, 1800000000))
            path.chmod(0o600)
        self.package()
        self.assertEqual(first, archive.read_bytes())
        checked, assets = release.validate_package(self.dist)
        self.assertEqual(checked, manifest)
        self.assertEqual({p.name for p in assets}, {archive.name, "SHA256SUMS", "release-manifest.json"})
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.namelist(), sorted(zipped.namelist()))
            self.assertEqual(
                {Path(name).name for name in zipped.namelist()},
                {"NotoBene-Regular.ttf", "OFL.txt", "FONT-NOTICE.txt"},
            )
            self.assertEqual(len(zipped.namelist()), 3)
            for info in zipped.infolist():
                self.assertEqual(info.date_time, (2026, 10, 16, 0, 0, 0))
                self.assertEqual(info.external_attr >> 16, 0o100644)
        external = json.loads((self.dist / "release/release-manifest.json").read_bytes())
        self.assertEqual(external, manifest)
        self.assertEqual(external["source_sha"], "b" * 40)
        self.assertEqual(external["noto_revision"], self.lock["revision"])
        self.assertEqual(external["noto_repository"], self.lock["repository"])
        self.assertEqual(external["source_date_epoch"], self.report["source_date_epoch"])
        self.assertEqual(set(external["toolchain"]),
                         {"python", "implementation", "fonttools", "skia-pathops", "pillow", "zlib"})
        self.assertEqual(external["noto_files"], {
            role: {key: entry[key] for key in ("sha256", "size")}
            for role, entry in self.lock["files"].items()
        })
        self.assertEqual(set(external["files"]),
                         {"NotoBene-Regular.ttf", "OFL.txt", "FONT-NOTICE.txt"})

    def test_build_metadata_required_for_packaging_but_not_package_validation(self):
        manifest = self.package()
        for name in ("noto.lock.json", "build-report.json", "validation.json"):
            metadata = self.dist / name
            original = metadata.read_bytes()
            metadata.unlink()
            with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                self.package()
            checked, _ = release.validate_package(self.dist)
            self.assertEqual(checked, manifest)
            metadata.write_bytes(original)

    def test_bad_build_or_validation_stops_packaging(self):
        for key, value in (("font_sha256", "0" * 64), ("source_sha256", "0" * 64), ("version", "9.999")):
            with self.subTest(key=key):
                report = dict(self.report, **{key: value})
                self.write_json("build-report.json", report)
                with self.assertRaises(ValueError):
                    self.package()
        self.write_json("build-report.json", self.report)
        for validation in ({"ok": False, "source_checked": True}, {"ok": True, "source_checked": False}):
            self.write_json("validation.json", validation)
            with self.assertRaises(ValueError):
                self.package()

    def test_modified_license_or_lock_is_rejected(self):
        (self.dist / "OFL.txt").write_bytes(b"altered-license")
        with self.assertRaisesRegex(ValueError, "license"):
            self.package()
        (self.dist / "OFL.txt").write_bytes((release.ROOT / "sources/OFL.txt").read_bytes())
        lock = copy.deepcopy(self.lock)
        lock["files"]["font"]["size"] += 1
        self.write_json("noto.lock.json", lock)
        with self.assertRaisesRegex(ValueError, "repository lock"):
            self.package()

    def test_stale_validation_after_rebuild_is_rejected(self):
        (self.dist / "NotoBene-Regular.ttf").write_bytes(b"rebuilt-font")
        self.report["font_sha256"] = release.file_record(b"rebuilt-font")["sha256"]
        self.write_json("build-report.json", self.report)
        with self.assertRaisesRegex(ValueError, "validation report"):
            self.package()

    def test_validation_against_another_source_is_rejected(self):
        self.validation["source_sha256"] = "0" * 64
        self.write_json("validation.json", self.validation)
        with self.assertRaisesRegex(ValueError, "validation report"):
            self.package()

    def test_zip_and_manifest_tampering_is_detected(self):
        manifest = self.package()
        archive = self.dist / "release" / manifest["archive"]
        original = archive.read_bytes()
        archive.write_bytes(original + b"corruption")
        with self.assertRaisesRegex(ValueError, "ZIP checksum"):
            release.validate_package(self.dist)
        archive.write_bytes(original)
        metadata = self.dist / "release/release-manifest.json"
        changed = copy.deepcopy(manifest)
        changed["files"]["NotoBene-Regular.ttf"]["sha256"] = "0" * 64
        metadata.write_bytes(release.json_bytes(changed))
        with self.assertRaisesRegex(ValueError, "payload checksum"):
            release.validate_package(self.dist)

    def test_external_provenance_must_match_builder_source(self):
        manifest = self.package()
        metadata = self.dist / "release/release-manifest.json"
        altered_noto = manifest["noto_revision"][:12] + "0" * 28
        altered_files = copy.deepcopy(manifest["noto_files"])
        altered_files["font"]["size"] += 1
        for key, value in (
            ("noto_revision", altered_noto),
            ("noto_repository", "https://example.com/other/noto"),
            ("noto_files", altered_files),
            ("font_version", "9.999"),
        ):
            with self.subTest(key=key):
                metadata.write_bytes(release.json_bytes(dict(manifest, **{key: value})))
                with self.assertRaisesRegex(ValueError, "repository lock|font version"):
                    release.validate_package(self.dist)

    def test_external_manifest_inventory_must_match_the_three_release_files(self):
        manifest = self.package()
        metadata = self.dist / "release/release-manifest.json"
        for added in (False, True):
            changed = copy.deepcopy(manifest)
            if added:
                changed["files"]["noto.lock.json"] = release.file_record(b"{}")
            else:
                del changed["files"]["NotoBene-Regular.ttf"]
            with self.subTest(added=added):
                metadata.write_bytes(release.json_bytes(changed))
                with self.assertRaisesRegex(ValueError, "payload inventory"):
                    release.validate_package(self.dist)

    def test_unexpected_zip_files_rejected_even_with_updated_checksum(self):
        manifest = self.package()
        archive = self.dist / "release" / manifest["archive"]
        with zipfile.ZipFile(archive) as zipped:
            original = {name: zipped.read(name) for name in zipped.namelist()}
        for extra in ("noto.lock.json", "build-report.json", "release-manifest.json", "validation.json"):
            with self.subTest(extra=extra):
                data = dict(original, **{archive.stem + "/" + extra: b"{}"})
                self.rewrite_archive(archive, data)
                with self.assertRaisesRegex(ValueError, "ZIP inventory"):
                    release.validate_package(self.dist)
        del original[archive.stem + "/NotoBene-Regular.ttf"]
        self.rewrite_archive(archive, original)
        with self.assertRaisesRegex(ValueError, "ZIP inventory"):
            release.validate_package(self.dist)

    def test_payload_tampering_detected_even_with_updated_outer_checksum(self):
        manifest = self.package()
        archive = self.dist / "release" / manifest["archive"]
        with zipfile.ZipFile(archive) as zipped:
            data = {name: zipped.read(name) for name in zipped.namelist()}
        data[archive.stem + "/NotoBene-Regular.ttf"] = b"changed-font"
        self.rewrite_archive(archive, data)
        with self.assertRaisesRegex(ValueError, "payload checksum"):
            release.validate_package(self.dist)


if __name__ == "__main__":
    unittest.main()
