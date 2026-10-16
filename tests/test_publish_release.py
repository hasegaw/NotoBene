"""Publication tests use a fake GitHub API and never contact GitHub."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import publish_release as publisher
from scripts.release_safety import check_release_settings, check_repository_info


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        notes_directory = self.directory / "release"
        notes_directory.mkdir()
        (notes_directory / "release-notes.md").write_text("Release notes\n", encoding="utf-8")
        self.tag = "20261016-noto-abcdef123456-r01"
        self.sha = "a" * 40
        self.manifest = {"tag": self.tag, "repository": "owner/repo",
                         "source_sha": self.sha, "font_version": "2.000"}
        self.paths = []
        self.assets = []
        for index, name in enumerate(("font.zip", "SHA256SUMS", "release-manifest.json")):
            path = self.directory / name
            path.write_bytes(name.encode())
            self.paths.append(path)
            self.assets.append({"id": index + 1, "name": name, "state": "uploaded",
                                "size": len(name),
                                "digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest()})
        self.release = {"id": 10, "tag_name": self.tag, "draft": True}
        self.releases = []
        self.tags = []
        self.repository_info = {"full_name": "owner/repo", "fork": False}
        self.target = self.sha
        self.remote_assets = list(self.assets)
        self.api_calls = []
        self.commands = []
        fake_module = SimpleNamespace(validate_package=Mock(return_value=(self.manifest, self.paths)))
        for replacement in (
            patch.dict(sys.modules, {"scripts.release": fake_module}),
            patch.dict(os.environ, {"GH_REPO": "owner/repo", "EXPECTED_SOURCE_SHA": self.sha,
                                    "RELEASE_REPOSITORY": "owner/repo",
                                    "GITHUB_REPOSITORY": "owner/repo",
                                    "GITHUB_EVENT_NAME": "workflow_dispatch"}),
            patch.object(publisher, "_api", side_effect=self.api),
            patch.object(publisher, "_command", side_effect=self.command),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def api(self, path, **kwargs):
        self.api_calls.append((path, kwargs))
        if path == "repos/owner/repo":
            return self.repository_info
        if kwargs.get("method") == "POST":
            self.target = kwargs["payload"]["sha"]
            return {}
        if "/git/ref/tags/" in path:
            return None if self.target is None else {"object": {"type": "commit", "sha": self.target}}
        if "/releases/tags/" in path:
            raise AssertionError("The by-tag REST endpoint must not be used for drafts")
        if "/assets?" in path:
            return self.remote_assets
        if "/releases?" in path:
            return self.releases + ([] if self.release is None else [self.release])
        if "/tags?" in path:
            return self.tags
        raise AssertionError(path)

    def command(self, *args):
        self.commands.append(args)
        if args[:2] == ("release", "create"):
            self.assertTrue(Path(args[args.index("--notes-file") + 1]).is_file())
            self.release = {"id": 10, "tag_name": self.tag, "draft": True}
        if args[:2] == ("release", "upload"):
            self.remote_assets = list(self.assets)
        if args[0] == "api":
            index = int(args[1].rsplit("/", 1)[1]) - 1
            return self.paths[index].read_bytes()
        return b""

    def assert_no_mutations(self):
        self.assertFalse([call for call in self.api_calls if call[1].get("method") == "POST"])
        self.assertFalse([args for args in self.commands if args[0] == "release"])

    def test_environment_mismatch_stops_before_network(self):
        for variable in ("GH_REPO", "EXPECTED_SOURCE_SHA"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: "wrong"}):
                with self.assertRaises(ValueError):
                    publisher.publish(self.directory)
        self.assertEqual(self.api_calls, [])
        self.assert_no_mutations()

    def test_release_opt_in_is_required_before_network(self):
        with patch.dict(os.environ):
            del os.environ["RELEASE_REPOSITORY"]
            with self.assertRaisesRegex(ValueError, "RELEASE_REPOSITORY"):
                publisher.publish(self.directory)
        self.assertEqual(self.api_calls, [])
        self.assert_no_mutations()

    def test_settings_mismatches_and_disabled_schedule_stop_before_network(self):
        settings = ({"RELEASE_REPOSITORY": "other/repo"},
                    {"GITHUB_REPOSITORY": "other/repo"},
                    {"GITHUB_EVENT_NAME": "schedule", "ENABLE_SCHEDULED_RELEASES": "false"})
        for values in settings:
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaises(ValueError):
                    publisher.publish(self.directory)
        self.assertEqual(self.api_calls, [])
        self.assert_no_mutations()

    def test_mismatched_or_incomplete_metadata_stops_before_release_api(self):
        for info in (None, [], {}, {"fork": True},
                     {"full_name": "other/repo", "fork": False},
                     {"full_name": "other/repo", "fork": True}):
            with self.subTest(info=info):
                self.repository_info = info
                with self.assertRaisesRegex(ValueError, "configured name"):
                    publisher.publish(self.directory)
        self.assertTrue(all(path == "repos/owner/repo" for path, _ in self.api_calls))
        self.assert_no_mutations()

    def test_fork_metadata_does_not_prevent_publication(self):
        for info in ({"full_name": "owner/repo", "fork": True}, {"full_name": "owner/repo"}):
            with self.subTest(info=info):
                self.repository_info = info
                publisher.publish(self.directory)
                self.assertEqual(self.commands[-1][:5], ("release", "edit", self.tag, "--repo", "owner/repo"))
                self.assertIn("--draft=false", self.commands[-1])

    def test_wrong_existing_tag_prevents_all_writes(self):
        self.target = "b" * 40
        with self.assertRaisesRegex(RuntimeError, "another source commit"):
            publisher.publish(self.directory)
        self.assert_no_mutations()

    def test_missing_notes_stops_before_network(self):
        (self.directory / "release" / "release-notes.md").unlink()
        with self.assertRaises(FileNotFoundError):
            publisher.publish(self.directory)
        self.assertEqual(self.api_calls, [])
        self.assert_no_mutations()

    def test_missing_tag_and_conflicting_draft_target_prevent_all_writes(self):
        self.target = None
        self.release["target_commitish"] = "b" * 40
        with self.assertRaisesRegex(RuntimeError, "draft targets another source commit"):
            publisher.publish(self.directory)
        self.assert_no_mutations()

    def test_conflicting_draft_asset_prevents_tag_creation(self):
        self.target = None
        self.remote_assets[0] = dict(self.assets[0], digest="sha256:" + "0" * 64)
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            publisher.publish(self.directory)
        self.assert_no_mutations()

    def test_partial_draft_uploads_only_missing_asset_then_publishes(self):
        self.remote_assets = self.assets[:2]
        publisher.publish(self.directory)
        uploads = [args for args in self.commands if args[:2] == ("release", "upload")]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][-1], str(self.paths[2]))
        self.assertNotIn(str(self.paths[0]), uploads[0])
        self.assertIn("--draft=false", self.commands[-1])

    def test_identical_published_release_downloads_missing_digest_and_skips(self):
        self.release["draft"] = False
        self.remote_assets[0] = dict(self.assets[0], digest=None)
        publisher.publish(self.directory)
        self.assertEqual(self.commands[0][0], "api")
        self.assert_no_mutations()

    def test_incomplete_published_release_is_not_repaired(self):
        self.release["draft"] = False
        self.remote_assets = self.assets[:2]
        with self.assertRaisesRegex(RuntimeError, "missing its tag or required assets"):
            publisher.publish(self.directory)
        self.assert_no_mutations()

    def test_same_revision_on_different_date_is_rejected(self):
        self.releases = [{"tag_name": "20261015-noto-abcdef123456-r01", "draft": True}]
        with self.assertRaisesRegex(RuntimeError, "already have another tag"):
            publisher.publish(self.directory)
        self.assert_no_mutations()

    def test_duplicate_matching_releases_prevent_all_writes(self):
        self.releases = [dict(self.release, id=11)]
        with self.assertRaisesRegex(RuntimeError, "Multiple releases"):
            publisher.publish(self.directory)
        self.assert_no_mutations()

    def test_orphan_tag_on_different_date_prevents_all_writes(self):
        self.tags = [{"name": "20261015-noto-abcdef123456-r01"}]
        with self.assertRaisesRegex(RuntimeError, "already have another tag"):
            publisher.publish(self.directory)
        self.assert_no_mutations()

    def test_historical_release_does_not_replace_latest(self):
        self.releases = [{"tag_name": "20261017-noto-fedcba654321-r01", "draft": False}]
        publisher.publish(self.directory)
        self.assertIn("--latest=false", self.commands[-1])

    def test_new_release_creates_commit_tag_and_draft_before_upload(self):
        self.target = self.release = None
        self.remote_assets = []
        publisher.publish(self.directory)
        writes = [call for call in self.api_calls if call[1].get("method") == "POST"]
        self.assertEqual(writes[0][1]["payload"], {"ref": f"refs/tags/{self.tag}", "sha": self.sha})
        self.assertEqual([args[1] for args in self.commands], ["create", "upload", "edit"])
        self.assertIn("--verify-tag", self.commands[0])
        self.assertIn("--draft", self.commands[0])


class ApiStatusTests(unittest.TestCase):
    def test_only_confirmed_404_is_absent(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                result = subprocess.CompletedProcess([], 1, f"HTTP/2.0 {status} Error\n\n{{}}".encode(), b"")
                with patch.object(publisher, "_gh", return_value=result):
                    if status == 404:
                        self.assertIsNone(publisher._api("resource", missing_ok=True))
                    else:
                        with self.assertRaises(RuntimeError):
                            publisher._api("resource", missing_ok=True)

    def test_network_failure_is_not_absence(self):
        result = subprocess.CompletedProcess([], 1, b"", b"connection failed")
        with patch.object(publisher, "_gh", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "no verifiable HTTP status"):
                publisher._api("resource", missing_ok=True)


class ReleaseSafetyTests(unittest.TestCase):
    def test_schedule_requires_explicit_true(self):
        with patch.dict(os.environ, {"RELEASE_REPOSITORY": "owner/repo",
                                     "GITHUB_EVENT_NAME": "schedule"}, clear=True):
            for flag in (None, "", "false", "TRUE"):
                if flag is None:
                    os.environ.pop("ENABLE_SCHEDULED_RELEASES", None)
                else:
                    os.environ["ENABLE_SCHEDULED_RELEASES"] = flag
                with self.subTest(flag=flag), self.assertRaises(ValueError):
                    check_release_settings("owner/repo")
            os.environ["ENABLE_SCHEDULED_RELEASES"] = "true"
            check_release_settings("owner/repo")

    def test_repository_metadata_requires_exact_full_name(self):
        for info in (None, [], {}, {"fork": False}, {"full_name": "other/repo"}):
            with self.subTest(info=info), self.assertRaises(ValueError):
                check_repository_info("owner/repo", info)

    def test_repository_fork_metadata_is_not_required(self):
        check_repository_info("owner/repo", {"full_name": "owner/repo"})
        for fork in (False, True, None, 0):
            with self.subTest(fork=fork):
                check_repository_info("owner/repo", {"full_name": "owner/repo", "fork": fork})


if __name__ == "__main__":
    unittest.main()
