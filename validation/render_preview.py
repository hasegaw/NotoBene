#!/usr/bin/env python3
"""Render a standalone PNG comparison of the source and generated Noto Bene fonts."""

import argparse
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont


ROWS = (
    "ASCII Abc0123 !?@  /  日本語 漢字 かな カナ",
    "一 口 田 あ い う α β é ñ ©  ■ □",
)


def render_preview(font_path, source_path, output_path):
    """Write the comparison image without importing the font builder."""
    font_path, source_path, output_path = map(Path, (font_path, source_path, output_path))
    for path in (font_path, source_path):
        if not path.is_file():
            raise ValueError(f"Font file does not exist: {path}")
    if output_path.resolve() in (font_path.resolve(), source_path.resolve()):
        raise ValueError("The preview output must not overwrite either input font")

    source_font = ImageFont.truetype(str(source_path), 48)
    output_font = ImageFont.truetype(str(font_path), 48)
    label_font = ImageFont.truetype(str(source_path), 25)
    detail_font = ImageFont.truetype(str(font_path), 172)
    canvas = Image.new("RGB", (1400, 730), "#f4f5f7")
    draw = ImageDraw.Draw(canvas)
    foreground, label_color = "#111827", "#334155"

    draw.text((32, 20), "Noto Sans CJK JP — 元のグリフ", font=label_font, fill=label_color)
    for text, y in zip(ROWS, (63, 133)):
        draw.text((32, y), text, font=source_font, fill=foreground)

    draw.text(
        (32, 235), "Noto Bene Regular 2.000 — 非ASCIIに丸囲み！",
        font=label_font, fill=label_color,
    )
    for text, y in zip(ROWS, (278, 348)):
        draw.text((32, y), text, font=output_font, fill=foreground)

    draw.text(
        (32, 454), "拡大：円形に切り抜き、丸囲み！を合成",
        font=label_font, fill=label_color,
    )
    for index, character in enumerate("語機éＡ"):
        draw.text((55 + 320 * index, 500), character, font=detail_font, fill=foreground)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    return output_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True, help="Generated Noto Bene TTF")
    parser.add_argument("--source", type=Path, required=True, help="Original Noto font")
    parser.add_argument("--output", type=Path, required=True, help="Output PNG path")
    args = parser.parse_args(argv)
    try:
        output = render_preview(args.font, args.source, args.output)
    except (OSError, ValueError) as exc:
        print(f"render_preview: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
