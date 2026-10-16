"""Exercise latest-Noto selection and snapshot writes without remote mutations."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_release as prepare


class PrepareReleaseTests(unittest.TestCase):
    def setUp(self):
        original_lock = (prepare.ROOT / "sources/noto.lock.json").read_bytes()
        original_license = (prepare.ROOT / "sources/OFL.txt").read_bytes()
        temporary = tempfile.TemporaryDirectory(prefix="noto-prepare-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "sources").mkdir()
        (self.root / "sources/noto.lock.json").write_bytes(original_lock)
        (self.root / "sources/OFL.txt").write_bytes(original_license)
        (self.root / "sources/font-version.txt").write_text("2.000\n")
        self.repository = "example/noto-bene"
        self.head = "a" * 40
        self.upstream_sha = "b" * 40
        self.snapshot_sha = "c" * 40
        self.tag_target = "d" * 40
        self.default_branch = "main"
        self.created_at = "2026-10-15T16:30:00Z"
        self.content = {"font": b"OTTO-latest-font", "license": b"OFL-latest-license"}
        self.reference = None
        self.remote_snapshot = None
        self.releases = []
        self.assets = {}
        self.asset_bytes = {}
        self.committed = False
        self.repo_info = {"full_name": self.repository, "fork": False}
        self.dirty = ""
        environment = {
            "GH_REPO": self.repository, "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": self.repository, "RELEASE_REPOSITORY": self.repository,
            "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": self.head,
            "GITHUB_RUN_ID": "123", "GITHUB_EVENT_NAME": "workflow_dispatch",
        }
        for context in (patch.object(prepare, "ROOT", self.root), patch.dict(os.environ, environment, clear=True)):
            context.start()
            self.addCleanup(context.stop)
        self.api = self.mock("_api", self.api_reply)
        self.pages = self.mock("_pages", self.pages_reply)
        self.command = self.mock("_command", self.command_reply)
        self.git = self.mock("_git", self.git_reply)
        self.download = self.mock("_download", self.download_reply)
        self.tag_commit = self.mock("_tag_commit", lambda base, tag: self.tag_target)

    def mock(self, name, callback):
        context = patch.object(prepare, name, side_effect=callback)
        self.addCleanup(context.stop)
        return context.start()

    def api_reply(self, path, **options):
        if path == f"repos/{self.repository}":
            return self.repo_info
        if path == f"repos/{prepare.UPSTREAM}":
            return {"full_name": prepare.UPSTREAM, "default_branch": self.default_branch}
        if path.startswith(f"repos/{prepare.UPSTREAM}/git/ref/heads/"):
            return {"object": {"type": "commit", "sha": self.upstream_sha}}
        if path == f"repos/{self.repository}/actions/runs/123":
            return {"created_at": self.created_at}
        if path.startswith(f"repos/{self.repository}/git/ref/heads/"):
            self.assertTrue(options["missing_ok"])
            return self.reference
        raise AssertionError(f"Unexpected API request: {path}")

    def pages_reply(self, path):
        if path == f"repos/{self.repository}/releases":
            return self.releases
        release_id = int(path.split("/")[-2])
        return self.assets[release_id]

    def command_reply(self, *arguments):
        self.assertEqual(arguments[0], "api")
        self.assertEqual(arguments[2:], ("--header", "Accept: application/octet-stream"))
        return self.asset_bytes[int(arguments[1].split("/")[-1])]

    def git_reply(self, *arguments, env=None):
        if arguments == ("rev-parse", "HEAD"):
            return self.snapshot_sha if self.committed else self.head
        if arguments == ("status", "--porcelain"):
            return self.dirty
        if arguments == ("rev-parse", "FETCH_HEAD"):
            return self.snapshot_sha
        if arguments[0] == "checkout" and self.remote_snapshot is not None:
            for name, data in self.remote_snapshot.items():
                (self.root / name).write_bytes(data)
        if arguments[0] == "commit":
            self.committed = True
        return ""

    def download_reply(self, url, maximum):
        for role, (_, path, limit) in prepare.INPUTS.items():
            if url == f"https://raw.githubusercontent.com/{prepare.UPSTREAM}/{self.upstream_sha}/{path}":
                self.assertEqual(maximum, limit)
                return self.content[role]
        raise AssertionError(f"Unexpected download: {url}")

    def candidate_lock(self, revision=None):
        revision = revision or self.upstream_sha
        return {
            "schema_version": 1, "family": "Noto Sans CJK JP", "style": "Regular",
            "repository": prepare.UPSTREAM_URL, "revision": revision, "license": "OFL-1.1",
            "files": {
                role: {
                    "filename": filename,
                    "url": f"https://raw.githubusercontent.com/{prepare.UPSTREAM}/{revision}/{path}",
                    **prepare.file_record(self.content[role]),
                }
                for role, (filename, path, _) in prepare.INPUTS.items()
            },
        }

    def published(self, same_inputs=True, version="2.003"):
        upstream = "e" * 40
        tag, archive = prepare.release_identity("20261001", 1, upstream)
        manifest = {
            "schema_version": 1, "repository": self.repository, "source_sha": self.tag_target,
            "tag": tag, "archive": archive, "release_date": "20261001", "revision": 1,
            "noto_repository": prepare.UPSTREAM_URL, "noto_revision": upstream,
            "noto_files": {role: prepare.file_record(data) for role, data in self.content.items()},
            "font_version": version,
        }
        if not same_inputs:
            manifest["noto_files"]["font"] = prepare.file_record(b"previous-font")
        self.releases = [{"id": 1, "tag_name": tag, "draft": False}]
        self.set_manifest(manifest)
        return manifest

    def set_manifest(self, manifest):
        data = prepare.json_bytes(manifest)
        self.assets[1] = [{"id": 2, "name": "release-manifest.json", "state": "uploaded", "size": len(data)}]
        self.asset_bytes[2] = data

    def existing_snapshot(self):
        lock = self.candidate_lock("f" * 40)
        self.reference = {"object": {"type": "commit", "sha": self.snapshot_sha}}
        self.remote_snapshot = {
            "sources/noto.lock.json": prepare.json_bytes(lock),
            "sources/OFL.txt": self.content["license"],
            "sources/font-version.txt": b"2.007\n",
            "sources/release-request.json": prepare.json_bytes({"release_date": "20261001", "revision": 1}),
        }

    def assert_no_git_writes(self):
        self.assertTrue(all(call.args[0] in {"rev-parse", "status"} for call in self.git.call_args_list))

    def test_missing_or_mismatched_opt_in_is_rejected_before_network(self):
        for repository in ("", "other/repo"):
            with self.subTest(repository=repository), patch.dict(os.environ, {"RELEASE_REPOSITORY": repository}):
                with self.assertRaisesRegex(ValueError, "RELEASE_REPOSITORY"):
                    prepare.prepare("latest-noto")
        self.api.assert_not_called()
        self.git.assert_not_called()
        self.download.assert_not_called()

    def test_mismatched_repository_metadata_is_rejected_before_writes(self):
        self.repo_info = {"full_name": "other/repo", "fork": True}
        with self.assertRaisesRegex(ValueError, "configured name"):
            prepare.prepare("latest-noto")
        self.git.assert_not_called()
        self.download.assert_not_called()

    def test_fork_can_prepare_release(self):
        self.repo_info["fork"] = True
        result = prepare.prepare("latest-noto")
        self.assertEqual(result, {"ready": "true", "source_sha": self.snapshot_sha, "release_date": "20261016", "revision": "1"})
        self.assertTrue(self.committed)

    def test_schedule_needs_explicit_enablement(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            with self.assertRaisesRegex(ValueError, "ENABLE_SCHEDULED_RELEASES"):
                prepare.prepare("latest-noto")
        self.api.assert_not_called()
        self.git.assert_not_called()

    def test_checkout_must_be_main_clean_and_match_event_sha(self):
        for environment in ({"GITHUB_REF": "refs/heads/other"}, {"GITHUB_SHA": "0" * 40}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                with self.assertRaises(ValueError):
                    prepare.prepare("latest-noto")
        self.dirty = " M sources/noto.lock.json"
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            prepare.prepare("latest-noto")
        self.assert_no_git_writes()
        self.download.assert_not_called()

    def test_pinned_uses_exact_commit_and_requires_explicit_valid_date(self):
        for date in ("", "20260230"):
            with self.subTest(date=date), self.assertRaises(ValueError):
                prepare.prepare("pinned", date)
        result = prepare.prepare("pinned", "20261016", "02")
        self.assertEqual(result, {"ready": "true", "source_sha": self.head, "release_date": "20261016", "revision": "2"})
        self.assert_no_git_writes()
        self.download.assert_not_called()
        self.pages.assert_not_called()

    def test_unchanged_published_inputs_skip_despite_new_upstream_commit(self):
        self.published()
        self.default_branch = "release/current"
        result = prepare.prepare("latest-noto")
        self.assertEqual(result, {"ready": "false"})
        self.assert_no_git_writes()
        self.api.assert_any_call(f"repos/{prepare.UPSTREAM}/git/ref/heads/release%2Fcurrent")
        self.assertEqual(self.download.call_count, 2)
        self.assertFalse(any("actions/runs" in call.args[0] for call in self.api.call_args_list))

    def test_first_publication_creates_only_detached_snapshot_at_stable_jst_date(self):
        result = prepare.prepare("latest-noto")
        self.assertEqual(result, {"ready": "true", "source_sha": self.snapshot_sha, "release_date": "20261016", "revision": "1"})
        self.assertEqual(json.loads((self.root / "sources/noto.lock.json").read_bytes()), self.candidate_lock())
        self.assertEqual((self.root / "sources/OFL.txt").read_bytes(), self.content["license"])
        self.assertEqual((self.root / "sources/font-version.txt").read_text(), "2.000\n")
        self.assertEqual(json.loads((self.root / "sources/release-request.json").read_bytes()),
                         {"release_date": "20261016", "revision": 1})
        commands = [call.args for call in self.git.call_args_list]
        self.assertLess(commands.index(("checkout", "--detach", self.head)),
                        next(i for i, args in enumerate(commands) if args[0] == "commit"))
        self.assertIn(("add", "--", *prepare.SNAPSHOT_FILES), commands)
        branch = "codex/noto-release/" + "-".join(prepare.file_record(self.content[role])["sha256"] for role in prepare.INPUTS)
        self.assertIn(("push", "origin", f"HEAD:refs/heads/{branch}"), commands)
        self.assertTrue(all("--force" not in command and "--delete" not in command for command in commands))
        commit = next(call for call in self.git.call_args_list if call.args[0] == "commit")
        self.assertEqual(commit.kwargs["env"]["GIT_AUTHOR_DATE"], "2026-10-15T16:30:00+00:00")
        self.assertEqual(commit.kwargs["env"]["GIT_COMMITTER_DATE"], "2026-10-15T16:30:00+00:00")

    def test_changed_source_bumps_above_current_and_published_versions(self):
        self.published(same_inputs=False, version="2.003")
        (self.root / "sources/font-version.txt").write_text("2.010\n")
        prepare.prepare("latest-noto")
        self.assertEqual((self.root / "sources/font-version.txt").read_text(), "2.011\n")

    def test_new_version_exceeds_a_newer_published_version(self):
        self.published(same_inputs=False, version="3.999")
        prepare.prepare("latest-noto")
        self.assertEqual((self.root / "sources/font-version.txt").read_text(), "4.000\n")

    def test_existing_snapshot_keeps_source_date_and_version_across_later_runs(self):
        self.existing_snapshot()
        self.created_at = "2026-11-20T12:00:00Z"
        result = prepare.prepare("latest-noto")
        self.assertEqual(result, {"ready": "true", "source_sha": self.snapshot_sha, "release_date": "20261001", "revision": "1"})
        self.assertEqual((self.root / "sources/font-version.txt").read_text(), "2.007\n")
        self.assertFalse(any(call.args[0] in {"commit", "push", "add"} for call in self.git.call_args_list))
        self.assertFalse(any("actions/runs" in call.args[0] for call in self.api.call_args_list))

    def test_existing_snapshot_with_different_content_fails_without_push(self):
        self.existing_snapshot()
        self.remote_snapshot["sources/OFL.txt"] = b"wrong-license"
        with self.assertRaisesRegex(ValueError, "snapshot license"):
            prepare.prepare("latest-noto")
        self.assertFalse(any(call.args[0] in {"commit", "push", "add"} for call in self.git.call_args_list))

    def test_product_release_with_missing_or_malformed_manifest_fails_closed(self):
        original = self.published()
        for field, value in (("repository", "other/repo"), ("noto_repository", "https://example.com"),
                             ("schema_version", 2), ("tag", "other-tag"), ("noto_files", {}),
                             ("source_sha", "0" * 40), ("font_version", "NaN")):
            manifest = copy.deepcopy(original)
            manifest[field] = value
            self.set_manifest(manifest)
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare.prepare("latest-noto")
        self.assets[1] = []
        with self.assertRaisesRegex(ValueError, "no complete release manifest"):
            prepare.prepare("latest-noto")
        self.assert_no_git_writes()

    def test_unrelated_and_draft_releases_do_not_count_as_published(self):
        self.releases = [{"id": 1, "tag_name": "unrelated-v1", "draft": False},
                         {"id": 2, "tag_name": "20261001-noto-eeeeeeeeeeee-r01", "draft": True}]
        prepare.prepare("latest-noto")
        self.assertEqual((self.root / "sources/font-version.txt").read_text(), "2.000\n")

    def test_latest_rejects_manual_date_or_revision(self):
        for date, revision in (("20261016", "1"), ("", "2"), ("", "")):
            with self.subTest(date=date, revision=revision), self.assertRaises(ValueError):
                prepare.prepare("latest-noto", date, revision)
        self.assert_no_git_writes()
        self.download.assert_not_called()

    def test_cli_outputs_the_resolved_commit_without_network_writes(self):
        output = self.root / "outputs"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            self.assertEqual(prepare.main(["--mode", "pinned", "--release-date", "20261016"]), 0)
        self.assertEqual(output.read_text(), f"ready=true\nsource_sha={self.head}\nrelease_date=20261016\nrevision=1\n")
        self.assert_no_git_writes()


class InputDownloadTests(unittest.TestCase):
    def test_download_enforces_size_and_https_official_host(self):
        url = "https://raw.githubusercontent.com/notofonts/noto-cjk/commit/Sans/LICENSE"
        for final_url, content, valid in (
            (url, b"OFL", True), (url, b"", False), (url, b"x" * 33, False),
            (url.replace("https:", "http:"), b"OFL", False),
            ("https://example.com/LICENSE", b"OFL", False),
        ):
            with self.subTest(final_url=final_url, size=len(content)), patch.object(prepare, "urlopen") as opened:
                response = opened.return_value.__enter__.return_value
                response.geturl.return_value = final_url
                response.read.return_value = content
                if valid:
                    self.assertEqual(prepare._download(url, 32), content)
                    response.read.assert_called_once_with(33)
                else:
                    with self.assertRaises(ValueError):
                        prepare._download(url, 32)


if __name__ == "__main__":
    unittest.main()
