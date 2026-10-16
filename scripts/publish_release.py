#!/usr/bin/env python3
"""Publish a validated release package without replacing tags or assets."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

from scripts.release_safety import check_release_settings, check_repository_info


TAG_PATTERN = re.compile(r"^(\d{8})-noto-([0-9a-f]{12})-r(\d{2,})$")


def _gh(*arguments, data=None):
    result = subprocess.run(
        ["gh", *arguments], input=data, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    return result


def _command(*arguments):
    result = _gh(*arguments)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def _api(path, *, method="GET", payload=None, missing_ok=False):
    arguments = ["api", "--include", "--method", method, path]
    data = None
    if payload is not None:
        arguments += ["--input", "-"]
        data = json.dumps(payload).encode("utf-8")
    result = _gh(*arguments, data=data)
    response = result.stdout.decode("utf-8").replace("\r\n", "\n")
    headers, separator, body = response.partition("\n\n")
    status = re.match(r"HTTP/\S+ (\d{3})(?:\s|$)", headers)
    if not separator or status is None:
        raise RuntimeError("GitHub API returned no verifiable HTTP status")
    code = int(status.group(1))
    if code == 404 and missing_ok:
        return None
    if result.returncode or not 200 <= code < 300:
        raise RuntimeError(f"GitHub API {method} {path} failed (HTTP {code})")
    return json.loads(body) if body.strip() else None


def _pages(path):
    results = []
    page = 1
    while True:
        batch = _api(f"{path}?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RuntimeError(f"Expected a GitHub API list: {path}")
        results.extend(batch)
        if len(batch) < 100:
            return results
        page += 1


def _tag_commit(base, tag):
    reference = _api(f"{base}/git/ref/tags/{quote(tag, safe='')}", missing_ok=True)
    if reference is None:
        return None
    target = reference["object"]
    for _ in range(16):
        if target["type"] == "commit":
            return target["sha"]
        if target["type"] != "tag":
            break
        target = _api(f"{base}/git/tags/{target['sha']}")["object"]
    raise RuntimeError("Release tag does not resolve to a commit")


def _find_release(releases, tag):
    matches = [release for release in releases if release["tag_name"] == tag]
    if len(matches) > 1:
        raise RuntimeError("Multiple releases use the requested tag")
    return matches[0] if matches else None


def _assets(base, release, expected):
    """Reject conflicts before uploads; return names still missing."""
    seen = set()
    for asset in _pages(f"{base}/releases/{release['id']}/assets"):
        name = asset["name"]
        if name not in expected or name in seen:
            raise RuntimeError(f"Unexpected or duplicate release asset: {name}")
        seen.add(name)
        digest, size = expected[name]
        if asset.get("state") != "uploaded" or asset.get("size") != size:
            raise RuntimeError(f"Conflicting or incomplete release asset: {name}")
        remote_digest = asset.get("digest")
        if remote_digest is None:
            content = _command(
                "api", f"{base}/releases/assets/{asset['id']}",
                "--header", "Accept: application/octet-stream",
            )
            remote_digest = "sha256:" + hashlib.sha256(content).hexdigest()
            if len(content) != size:
                raise RuntimeError(f"Release asset size mismatch: {name}")
        if remote_digest != "sha256:" + digest:
            raise RuntimeError(f"Release asset digest mismatch: {name}")
    return set(expected) - seen


def publish(dist_dir: Path):
    from scripts.release import validate_package

    manifest, paths = validate_package(dist_dir)
    repository = manifest["repository"]
    source_sha = manifest["source_sha"]
    if os.environ.get("GH_REPO") != repository:
        raise ValueError("GH_REPO must equal the package repository")
    if os.environ.get("EXPECTED_SOURCE_SHA") != source_sha:
        raise ValueError("EXPECTED_SOURCE_SHA must equal the package source_sha")
    check_release_settings(repository)
    notes_path = Path(dist_dir) / "release" / "release-notes.md"
    notes_path.read_text(encoding="utf-8")
    tag = manifest["tag"]
    identity = TAG_PATTERN.fullmatch(tag)
    if identity is None:
        raise ValueError("Invalid release tag")
    base = f"repos/{repository}"
    check_repository_info(repository, _api(base))
    releases = _pages(f"{base}/releases")
    tag_names = {item["tag_name"] for item in releases}
    tag_names.update(item["name"] for item in _pages(f"{base}/tags"))
    for other_tag in tag_names:
        other = TAG_PATTERN.fullmatch(other_tag)
        if other is None:
            continue
        if (other[2] == identity[2] and int(other[3]) == int(identity[3])
                and other_tag != tag):
            raise RuntimeError("This Noto revision and release revision already have another tag")
    latest = True
    for item in releases:
        other = TAG_PATTERN.fullmatch(item["tag_name"])
        if other is not None and not item["draft"] and other[1] > identity[1]:
            latest = False
    # The by-tag REST endpoint excludes drafts; the authenticated list includes them.
    release = _find_release(releases, tag)
    target = _tag_commit(base, tag)
    if target is not None and target != source_sha:
        raise RuntimeError("Existing release tag points to another source commit")
    if release is not None and target is None:
        claimed_target = release.get("target_commitish", "")
        if re.fullmatch(r"[0-9a-f]{40}", claimed_target) and claimed_target != source_sha:
            raise RuntimeError("Existing draft targets another source commit")
    expected = {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
        for path in paths
    }
    missing = set(expected) if release is None else _assets(base, release, expected)
    if release is not None and not release["draft"]:
        if target is None or missing:
            raise RuntimeError("Published release is missing its tag or required assets")
        print(f"Release {tag} already published with identical assets")
        return
    if target is None:
        _api(f"{base}/git/refs", method="POST",
             payload={"ref": f"refs/tags/{tag}", "sha": source_sha})
    if release is None:
        _command(
            "release", "create", tag, "--repo", repository, "--verify-tag", "--draft",
            "--title", f"Noto Bene {manifest['font_version']} ({tag})",
            "--notes-file", str(notes_path),
        )
        release = _find_release(_pages(f"{base}/releases"), tag)
        if release is None or not release["draft"]:
            raise RuntimeError("Created draft release could not be confirmed")
    if missing:
        _command("release", "upload", tag, "--repo", repository,
                 *(str(path) for path in paths if path.name in missing))
    if _assets(base, release, expected):
        raise RuntimeError("Release assets are missing after upload")
    if _tag_commit(base, tag) != source_sha:
        raise RuntimeError("Release tag changed before publication")
    _command("release", "edit", tag, "--repo", repository, "--draft=false",
             f"--latest={str(latest).lower()}")
    print(f"Published release {tag}")
