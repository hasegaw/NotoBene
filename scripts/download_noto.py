#!/usr/bin/env python3
"""Fetch the immutable Noto inputs, checking SHA-256 before atomic installation.

This script only needs the Python standard library. Paths given on the command
line are relative to the working directory; defaults are relative to the repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CHUNK_SIZE = 1024 * 1024


def load_manifest(path: Path) -> dict:
    """Load the pinned font/license manifest and reject unsafe target names."""
    with path.open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")
    revision = manifest.get("revision", "")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("manifest revision must be a full Git commit SHA")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"font", "license"}:
        raise ValueError("manifest files must contain font and license")
    names: set[str] = set()
    for role, entry in files.items():
        if not isinstance(entry, dict):
            raise ValueError(f"invalid {role} entry")
        filename = entry.get("filename")
        if (not isinstance(filename, str) or not filename or
                filename in {".", ".."} or "/" in filename or "\\" in filename or
                Path(filename).name != filename):
            raise ValueError(f"unsafe {role} filename")
        if filename in names:
            raise ValueError("manifest filenames must be unique")
        names.add(filename)
        digest = entry.get("sha256", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid {role} SHA-256")
        size = entry.get("size")
        if type(size) is not int or size <= 0:
            raise ValueError(f"invalid {role} size")
        url = entry.get("url")
        if not isinstance(url, str):
            raise ValueError(f"invalid {role} URL")
        parsed = urlparse(url)
        if (parsed.scheme != "https" or not parsed.netloc or
                f"/{revision}/" not in parsed.path):
            raise ValueError(f"{role} URL must use HTTPS and the pinned revision")
    return manifest


def verified(path: Path, entry: dict) -> bool:
    """Check both length and digest, including for a previously cached file."""
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest() == entry["sha256"]


def download_file(entry: dict, destination: Path, timeout: float = 60) -> None:
    """Install one file only after a fully downloaded temporary file validates."""
    request = Request(entry["url"], headers={"User-Agent": "noto-bene-builder/1"})
    temporary: Path | None = None
    try:
        with urlopen(request, timeout=timeout) as response:
            if urlparse(response.geturl()).scheme != "https":
                raise ValueError("download redirected to a non-HTTPS URL")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", suffix=".part",
                dir=destination.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                received = 0
                while chunk := response.read(CHUNK_SIZE):
                    received += len(chunk)
                    if received > entry["size"]:
                        raise ValueError(f"download exceeds pinned size: {destination.name}")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if not verified(temporary, entry):
            raise ValueError(f"size or SHA-256 mismatch: {destination.name}")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_sources(
    manifest_path: Path = ROOT / "sources/noto.lock.json",
    output_dir: Path = ROOT / "vendor/noto",
    *,
    offline: bool = False,
    timeout: float = 60,
) -> dict[str, Path]:
    """Return verified input paths, fetching missing or corrupt cache entries."""
    manifest = load_manifest(Path(manifest_path))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for role, entry in manifest["files"].items():
        destination = output_dir / entry["filename"]
        if verified(destination, entry):
            print(f"Verified cache: {destination}")
        elif offline:
            raise ValueError(f"missing or corrupt cached {role}: {destination}")
        else:
            print(f"Downloading {role}: {entry['url']}")
            for attempt in range(3):
                try:
                    download_file(entry, destination, timeout=timeout)
                    break
                except (URLError, TimeoutError, ConnectionError):
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            print(f"Verified download: {destination}")
        paths[role] = destination
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "sources/noto.lock.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "vendor/noto")
    parser.add_argument("--offline", action="store_true", help="verify cache without network access")
    parser.add_argument("--timeout", type=float, default=60, help="network timeout in seconds (default: 60)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        download_sources(args.manifest, args.output_dir, offline=args.offline, timeout=args.timeout)
    except (OSError, ValueError, URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
