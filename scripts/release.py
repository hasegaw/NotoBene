#!/usr/bin/env python3
"""Package a verified font reproducibly, or publish that exact package to GitHub."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re
import sys
import tempfile
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.download_noto import load_manifest

PAYLOAD_FILES = (
    "NotoBene-Regular.ttf", "OFL.txt", "FONT-NOTICE.txt",
)
MANIFEST_NAME = "release-manifest.json"


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def file_record(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def release_identity(release_date, revision, noto_revision):
    """Use a fixed publication date in JST and a two-digit per-Noto revision."""
    if not isinstance(release_date, str) or not re.fullmatch(r"[0-9]{8}", release_date):
        raise ValueError("release date must be YYYYMMDD (publication date in JST)")
    date = datetime.strptime(release_date, "%Y%m%d")
    if not 1980 <= date.year <= 2107:
        raise ValueError("release date must fit the ZIP date range (1980..2107)")
    if isinstance(revision, str) and re.fullmatch(r"[0-9]{1,2}", revision):
        revision = int(revision)
    if type(revision) is not int or not 1 <= revision <= 99:
        raise ValueError("revision must be an integer from 1 to 99")
    if not isinstance(noto_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", noto_revision):
        raise ValueError("Noto revision must be a full lowercase commit SHA")
    tag = f"{release_date}-noto-{noto_revision[:12]}-r{revision:02d}"
    return tag, f"NotoBene-{tag}.zip"


def validate_origin(repository, source_sha):
    if (not isinstance(repository, str) or
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or
            any(part in {".", ".."} for part in repository.split("/"))):
        raise ValueError("repository must be owner/repo")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source SHA must be a full lowercase commit SHA")


def package(dist_dir, release_date, revision, repository, source_sha):
    dist_dir = Path(dist_dir)
    validate_origin(repository, source_sha)
    lock = load_manifest(dist_dir / "noto.lock.json")
    # A release always uses the committed input manifest, never an ad-hoc override.
    if lock != load_manifest(ROOT / "sources/noto.lock.json"):
        raise ValueError("built Noto lock differs from the repository lock")
    tag, archive_name = release_identity(release_date, revision, lock["revision"])
    payload = {name: (dist_dir / name).read_bytes() for name in PAYLOAD_FILES}
    if any(not data for data in payload.values()):
        raise ValueError("release payload contains an empty file")
    report = json.loads((dist_dir / "build-report.json").read_text(encoding="utf-8"))
    validation = json.loads((dist_dir / "validation.json").read_text(encoding="utf-8"))
    if validation.get("ok") is not True or validation.get("source_checked") is not True:
        raise ValueError("a successful independent validation against Noto is required")
    if (validation.get("font_sha256") != file_record(payload["NotoBene-Regular.ttf"])["sha256"] or
            validation.get("source_sha256") != lock["files"]["font"]["sha256"]):
        raise ValueError("validation report does not describe this font and Noto source")
    if report.get("font_sha256") != file_record(payload["NotoBene-Regular.ttf"])["sha256"]:
        raise ValueError("font differs from the build report")
    if report.get("source_sha256") != lock["files"]["font"]["sha256"]:
        raise ValueError("build report source differs from the Noto lock")
    if file_record(payload["OFL.txt"]) != {key: lock["files"]["license"][key] for key in ("sha256", "size")}:
        raise ValueError("license differs from the Noto lock")
    configured_version = (ROOT / "sources/font-version.txt").read_text(encoding="utf-8").strip()
    if report.get("version") != configured_version:
        raise ValueError("built font version differs from sources/font-version.txt")
    manifest = {
        "schema_version": 1, "tag": tag, "archive": archive_name,
        "release_date": release_date, "date_timezone": "Asia/Tokyo",
        "revision": int(revision), "repository": repository, "source_sha": source_sha,
        "noto_repository": lock["repository"], "noto_revision": lock["revision"],
        "noto_files": {
            role: {key: entry[key] for key in ("sha256", "size")}
            for role, entry in lock["files"].items()
        },
        "font_version": report["version"],
        "source_date_epoch": report["source_date_epoch"],
        "toolchain": {
            "python": platform.python_version(), "implementation": platform.python_implementation(),
            "fonttools": version("fonttools"), "skia-pathops": version("skia-pathops"),
            "pillow": version("pillow"), "zlib": zlib.ZLIB_RUNTIME_VERSION,
        },
        "files": {name: file_record(data) for name, data in payload.items()},
    }
    manifest_data = json_bytes(manifest)
    date = datetime.strptime(release_date, "%Y%m%d")
    release_dir = dist_dir / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    # Only complete archives and metadata become visible to the publishing job.
    with tempfile.TemporaryDirectory(prefix=".package-", dir=release_dir) as temporary:
        staging = Path(temporary)
        archive = staging / archive_name
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for name, data in sorted(payload.items()):
                info = zipfile.ZipInfo(f"{archive.stem}/{name}", (date.year, date.month, date.day, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                output.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        checksum = file_record(archive.read_bytes())["sha256"]
        (staging / "SHA256SUMS").write_text(f"{checksum}  {archive_name}\n", encoding="utf-8")
        (staging / MANIFEST_NAME).write_bytes(manifest_data)
        notes = (
            f"Noto Bene {report['version']} — {release_date} (JST), r{int(revision):02d}\n\n"
            f"Install the .ttf from `{archive_name}`. Keep OFL.txt and FONT-NOTICE.txt with redistributions.\n\n"
            f"- Noto source: [{lock['revision']}]({lock['repository']}/commit/{lock['revision']})\n"
            f"- Builder source: [{source_sha}](https://github.com/{repository}/commit/{source_sha})\n"
            f"- ZIP SHA-256: `{checksum}`\n"
            "- Unit tests and independent validation against the pinned Noto source passed.\n\n"
            "The ZIP contains only the font, OFL.txt and FONT-NOTICE.txt. "
            "Provenance is in the separately attached release-manifest.json. "
            "The Noto lock, build report, comparison images and detailed validation report "
            "are in the workflow artifact.\n"
        )
        (staging / "release-notes.md").write_text(notes, encoding="utf-8")
        for name in (archive_name, "SHA256SUMS", MANIFEST_NAME, "release-notes.md"):
            (staging / name).replace(release_dir / name)
    validate_package(dist_dir)
    return manifest


def validate_package(dist_dir):
    """Check every byte of the package before any GitHub mutation."""
    release_dir = Path(dist_dir) / "release"
    manifest_path = release_dir / MANIFEST_NAME
    manifest_data = manifest_path.read_bytes()
    manifest = json.loads(manifest_data)
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported release manifest")
    validate_origin(manifest["repository"], manifest["source_sha"])
    tag, archive_name = release_identity(manifest["release_date"], manifest["revision"], manifest["noto_revision"])
    if manifest.get("tag") != tag or manifest.get("archive") != archive_name:
        raise ValueError("release identity does not match its manifest")
    if set(manifest["files"]) != set(PAYLOAD_FILES):
        raise ValueError("unexpected release payload inventory")
    # Publication checks out the builder commit first. Verify provenance against
    # its pinned inputs now that build metadata is kept outside the consumer ZIP.
    lock = load_manifest(ROOT / "sources/noto.lock.json")
    if (manifest.get("noto_repository") != lock["repository"] or
            manifest["noto_revision"] != lock["revision"]):
        raise ValueError("release Noto source differs from the repository lock")
    noto_files = {
        role: {key: entry[key] for key in ("sha256", "size")}
        for role, entry in lock["files"].items()
    }
    if manifest.get("noto_files") != noto_files:
        raise ValueError("release Noto input hashes differ from the repository lock")
    if manifest["files"]["OFL.txt"] != noto_files["license"]:
        raise ValueError("release license differs from the Noto lock")
    configured_version = (ROOT / "sources/font-version.txt").read_text(encoding="utf-8").strip()
    if manifest.get("font_version") != configured_version:
        raise ValueError("release font version differs from sources/font-version.txt")
    archive = release_dir / archive_name
    checksum_path = release_dir / "SHA256SUMS"
    expected_sum = f"{file_record(archive.read_bytes())['sha256']}  {archive_name}\n"
    if checksum_path.read_text(encoding="utf-8") != expected_sum:
        raise ValueError("ZIP checksum mismatch")
    with zipfile.ZipFile(archive) as zipped:
        prefix = archive.stem + "/"
        expected_names = {prefix + name for name in PAYLOAD_FILES}
        if set(zipped.namelist()) != expected_names or len(zipped.namelist()) != len(expected_names):
            raise ValueError("unexpected ZIP inventory")
        for name in PAYLOAD_FILES:
            if file_record(zipped.read(prefix + name)) != manifest["files"][name]:
                raise ValueError(f"ZIP payload checksum mismatch: {name}")
    return manifest, [archive, checksum_path, manifest_path]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack = subparsers.add_parser("package", help="create a deterministic release ZIP")
    pack.add_argument("--release-date", required=True, help="publication date in JST, YYYYMMDD")
    pack.add_argument("--revision", required=True, help="per-Noto distribution revision, 1..99")
    pack.add_argument("--repository", required=True, help="GitHub owner/repo")
    pack.add_argument("--source-sha", required=True, help="full builder commit SHA")
    pack.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    publish_parser = subparsers.add_parser("publish", help="publish verified assets without overwriting")
    publish_parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    if args.command == "package":
        result = package(args.dist_dir, args.release_date, args.revision, args.repository, args.source_sha)
        print(f"Packaged {result['archive']}")
    else:
        from scripts.publish_release import publish
        publish(args.dist_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"Release failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
