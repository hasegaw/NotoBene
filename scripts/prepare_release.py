#!/usr/bin/env python3
"""Resolve pinned or latest Noto inputs and prepare an immutable release snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.download_noto import load_manifest
from scripts.publish_release import TAG_PATTERN, _api, _command, _pages, _tag_commit
from scripts.release import file_record, json_bytes, release_identity, validate_origin
from scripts.release_safety import check_release_settings, check_repository_info

UPSTREAM = "notofonts/noto-cjk"
UPSTREAM_URL = f"https://github.com/{UPSTREAM}"
INPUTS = {
    "font": ("NotoSansCJKjp-Regular.otf", "Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf", 64 * 1024 * 1024),
    "license": ("OFL.txt", "Sans/LICENSE", 1024 * 1024),
}
SNAPSHOT_FILES = (
    "sources/noto.lock.json", "sources/OFL.txt",
    "sources/font-version.txt", "sources/release-request.json",
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
JST = timezone(timedelta(hours=9))


def _git(*arguments, env=None):
    result = subprocess.run(
        ["git", *arguments], cwd=ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8").strip()


def _download(url, maximum):
    request = Request(url, headers={"User-Agent": "noto-bene-release/1"})
    with urlopen(request, timeout=60) as response:
        target = urlparse(response.geturl())
        if target.scheme != "https" or target.hostname != "raw.githubusercontent.com":
            raise ValueError("Noto input redirected outside the official raw GitHub host")
        data = response.read(maximum + 1)
    if not data or len(data) > maximum:
        raise ValueError("Noto input is empty or exceeds its size limit")
    return data


def _input_records(lock):
    return {
        role: {key: lock["files"][role][key] for key in ("sha256", "size")}
        for role in INPUTS
    }


def _version_number(value):
    if (not isinstance(value, str) or
            not re.fullmatch(r"(?:0|[1-9][0-9]{0,4})\.[0-9]{3}", value)):
        raise ValueError("Invalid Noto Bene font version")
    major, minor = (int(part) for part in value.split("."))
    if major > 32767:
        raise ValueError("Font version exceeds the OpenType limit")
    return major * 1000 + minor


def _run_created_at(repository):
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("GITHUB_RUN_ID is required to fix the release date across reruns")
    run = _api(f"repos/{repository}/actions/runs/{run_id}")
    value = run.get("created_at")
    if not isinstance(value, str):
        raise ValueError("Workflow run has no creation timestamp")
    created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("Workflow run creation timestamp has no timezone")
    return created


def _latest_inputs():
    info = _api(f"repos/{UPSTREAM}")
    if info.get("full_name") != UPSTREAM or not isinstance(info.get("default_branch"), str):
        raise ValueError("Cannot identify the official Noto default branch")
    branch = info["default_branch"]
    if not branch:
        raise ValueError("The official Noto default branch is empty")
    reference = _api(f"repos/{UPSTREAM}/git/ref/heads/{quote(branch, safe='')}")
    target = reference["object"]
    revision = target.get("sha", "")
    if target.get("type") != "commit" or not SHA.fullmatch(revision):
        raise ValueError("The Noto default branch does not resolve to a full commit SHA")
    lock = {
        "schema_version": 1, "family": "Noto Sans CJK JP", "style": "Regular",
        "repository": UPSTREAM_URL, "revision": revision, "license": "OFL-1.1", "files": {},
    }
    content = {}
    for role, (filename, path, limit) in INPUTS.items():
        url = f"https://raw.githubusercontent.com/{UPSTREAM}/{revision}/{path}"
        content[role] = _download(url, limit)
        lock["files"][role] = {"filename": filename, "url": url, **file_record(content[role])}
    return lock, content


def _published_manifests(repository):
    manifests = []
    base = f"repos/{repository}"
    for release in _pages(f"{base}/releases"):
        if release["draft"] or not TAG_PATTERN.fullmatch(release["tag_name"]):
            continue
        assets = [item for item in _pages(f"{base}/releases/{release['id']}/assets")
                  if item["name"] == "release-manifest.json"]
        if (len(assets) != 1 or assets[0].get("state") != "uploaded" or
                type(assets[0].get("size")) is not int or not 0 < assets[0]["size"] <= 1024 * 1024):
            raise ValueError("Published Noto Bene release has no complete release manifest")
        asset = assets[0]
        data = _command("api", f"{base}/releases/assets/{asset['id']}",
                        "--header", "Accept: application/octet-stream")
        if len(data) != asset["size"]:
            raise ValueError("Published release manifest size mismatch")
        if asset.get("digest") not in (None, "sha256:" + file_record(data)["sha256"]):
            raise ValueError("Published release manifest digest mismatch")
        manifest = json.loads(data)
        if (manifest.get("schema_version") != 1 or manifest.get("repository") != repository or
                manifest.get("tag") != release["tag_name"]):
            raise ValueError("Published release manifest identity mismatch")
        validate_origin(repository, manifest.get("source_sha"))
        tag, _ = release_identity(manifest["release_date"], manifest["revision"], manifest["noto_revision"])
        if tag != manifest["tag"] or manifest.get("noto_repository") != UPSTREAM_URL:
            raise ValueError("Published release does not identify the official Noto source")
        if _tag_commit(base, tag) != manifest["source_sha"]:
            raise ValueError("Published release source commit differs from its tag")
        records = manifest.get("noto_files")
        if not isinstance(records, dict) or set(records) != set(INPUTS):
            raise ValueError("Published release manifest has no Noto input fingerprints")
        for record in records.values():
            if (not isinstance(record, dict) or set(record) != {"sha256", "size"} or
                    not isinstance(record["sha256"], str) or not DIGEST.fullmatch(record["sha256"]) or
                    type(record["size"]) is not int or record["size"] <= 0):
                raise ValueError("Published release has invalid Noto input fingerprints")
        _version_number(manifest.get("font_version"))
        manifests.append(manifest)
    return manifests


def _reuse_snapshot(branch, reference, candidate):
    target = reference["object"]
    sha = target.get("sha", "")
    if target.get("type") != "commit" or not SHA.fullmatch(sha):
        raise ValueError("Existing release snapshot does not point to a commit")
    _git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
    if _git("rev-parse", "FETCH_HEAD") != sha:
        raise ValueError("Release snapshot changed while fetching it")
    _git("checkout", "--detach", sha)
    lock = load_manifest(ROOT / "sources/noto.lock.json")
    if (lock.get("repository") != UPSTREAM_URL or _input_records(lock) != _input_records(candidate) or
            any(lock["files"][role]["url"] !=
                f"https://raw.githubusercontent.com/{UPSTREAM}/{lock['revision']}/{path}"
                for role, (_, path, _) in INPUTS.items())):
        raise ValueError("Existing release snapshot has different Noto inputs")
    if file_record((ROOT / "sources/OFL.txt").read_bytes()) != _input_records(candidate)["license"]:
        raise ValueError("Existing release snapshot license does not match its lock")
    _version_number((ROOT / "sources/font-version.txt").read_text(encoding="utf-8").strip())
    request = json.loads((ROOT / "sources/release-request.json").read_text(encoding="utf-8"))
    if set(request) != {"release_date", "revision"} or type(request["revision"]) is not int or request["revision"] != 1:
        raise ValueError("Existing release snapshot has an invalid release request")
    release_identity(request["release_date"], request["revision"], lock["revision"])
    return {"ready": "true", "source_sha": sha, "release_date": request["release_date"], "revision": "1"}


def prepare(mode, release_date="", revision="1"):
    repository = os.environ.get("GH_REPO", "")
    check_release_settings(repository)
    check_repository_info(repository, _api(f"repos/{repository}"))
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Release preparation must start from main")
    source_sha = _git("rev-parse", "HEAD")
    validate_origin(repository, source_sha)
    if source_sha != os.environ.get("GITHUB_SHA"):
        raise ValueError("Checkout does not match the workflow source commit")
    if _git("status", "--porcelain"):
        raise ValueError("Release preparation requires a clean checkout")
    if mode == "pinned":
        lock = load_manifest(ROOT / "sources/noto.lock.json")
        release_identity(release_date, revision, lock["revision"])
        return {"ready": "true", "source_sha": source_sha, "release_date": release_date, "revision": str(int(revision))}
    if mode != "latest-noto":
        raise ValueError("Unknown release preparation mode")
    if release_date or str(revision) not in {"1", "01"}:
        raise ValueError("latest-noto fixes the release date from its run and uses revision 1")
    candidate, content = _latest_inputs()
    published = _published_manifests(repository)
    if any(item["noto_files"] == _input_records(candidate) for item in published):
        return {"ready": "false"}
    branch = "codex/noto-release/" + "-".join(candidate["files"][role]["sha256"] for role in INPUTS)
    reference = _api(f"repos/{repository}/git/ref/heads/{quote(branch, safe='')}", missing_ok=True)
    if reference is not None:
        return _reuse_snapshot(branch, reference, candidate)
    created = _run_created_at(repository)
    date = created.astimezone(JST).strftime("%Y%m%d")
    release_identity(date, 1, candidate["revision"])
    number = _version_number((ROOT / "sources/font-version.txt").read_text(encoding="utf-8").strip())
    if published:
        number = max(number, *(_version_number(item["font_version"]) for item in published)) + 1
    font_version = f"{number // 1000}.{number % 1000:03d}"
    _version_number(font_version)
    # A detached snapshot prevents the automation from committing on main.
    _git("checkout", "--detach", source_sha)
    (ROOT / SNAPSHOT_FILES[0]).write_bytes(json_bytes(candidate))
    (ROOT / SNAPSHOT_FILES[1]).write_bytes(content["license"])
    (ROOT / SNAPSHOT_FILES[2]).write_text(font_version + "\n", encoding="utf-8")
    (ROOT / SNAPSHOT_FILES[3]).write_bytes(json_bytes({"release_date": date, "revision": 1}))
    _git("add", "--", *SNAPSHOT_FILES)
    commit_env = dict(os.environ, GIT_AUTHOR_NAME="github-actions[bot]", GIT_COMMITTER_NAME="github-actions[bot]",
                      GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
                      GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
                      GIT_AUTHOR_DATE=created.isoformat(), GIT_COMMITTER_DATE=created.isoformat())
    _git("commit", "-m", f"chore: pin Noto {candidate['revision'][:12]} for {date} release", env=commit_env)
    snapshot_sha = _git("rev-parse", "HEAD")
    validate_origin(repository, snapshot_sha)
    _git("push", "origin", f"HEAD:refs/heads/{branch}")
    return {"ready": "true", "source_sha": snapshot_sha, "release_date": date, "revision": "1"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("latest-noto", "pinned"))
    parser.add_argument("--release-date", default="", help="required for pinned mode: publication date in JST (YYYYMMDD)")
    parser.add_argument("--revision", default="1", help="pinned per-Noto revision; latest-noto always uses 1")
    args = parser.parse_args(argv)
    result = prepare(args.mode, args.release_date, args.revision)
    output = "".join(f"{key}={value}\n" for key, value in result.items())
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(output)
    print(output, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"Release preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
