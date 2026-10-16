#!/usr/bin/env python3
"""Download pinned Noto, build Noto Bene, and collect redistributable artifacts."""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "vendor" / "noto")
    parser.add_argument("--manifest", type=Path, default=ROOT / "sources" / "noto.lock.json")
    parser.add_argument("--offline", action="store_true", help="Require verified cached source files")
    args = parser.parse_args(argv)
    # Keep downloading usable even on a machine without build dependencies.
    import subprocess
    command = [sys.executable, str(ROOT / "scripts" / "download_noto.py"),
               "--manifest", str(args.manifest), "--output-dir", str(args.cache_dir)]
    if args.offline:
        command.append("--offline")
    subprocess.run(command, check=True)
    from noto_bene.build import build_font
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = build_font(args.cache_dir / manifest["files"]["font"]["filename"],
                        args.output_dir / "NotoBene-Regular.ttf",
                        args.output_dir / "build-report.json")
    shutil.copyfile(args.cache_dir / manifest["files"]["license"]["filename"],
                    args.output_dir / "OFL.txt")
    shutil.copyfile(args.manifest, args.output_dir / "noto.lock.json")
    shutil.copyfile(ROOT / "FONT-NOTICE.txt", args.output_dir / "FONT-NOTICE.txt")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ImportError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
