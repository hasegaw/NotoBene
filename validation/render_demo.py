#!/usr/bin/env python3
"""Render real ASCII/homoglyph strings with Noto and Noto Bene, and record code points."""

import argparse
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont

ASCII_TEXT = "amazon"
CONFUSABLE_TEXT = "\u0430m\u0430zon"  # Cyrillic small a at positions 0 and 2.


def render_demo(font_path, source_path, output_path, report_path=None):
    font_path, source_path, output_path = map(Path, (font_path, source_path, output_path))
    protected = {font_path.resolve(), source_path.resolve()}
    if output_path.resolve() in protected:
        raise ValueError("Image output must not overwrite an input font")
    if report_path is not None:
        report_path = Path(report_path)
        if report_path.resolve() in protected | {output_path.resolve()}:
            raise ValueError("Report output must not overwrite a font or the image")
    with TTFont(source_path) as source, TTFont(font_path) as output:
        source_map, output_map = source.getBestCmap(), output.getBestCmap()
        cases = []
        for text in (ASCII_TEXT, CONFUSABLE_TEXT):
            if any(ord(char) not in source_map for char in text):
                raise ValueError("Source font must contain every demo character, including U+0430")
            names = [output_map.get(ord(char), "") for char in text]
            marked = [i for i, name in enumerate(names) if name.startswith("marked.")]
            expected = [i for i, char in enumerate(text) if ord(char) >= 128]
            if marked != expected or any(
                name != f"ascii.{ord(char):04X}" for char, name in zip(text, names) if ord(char) < 128
            ):
                raise ValueError("Output font does not distinguish ASCII from the non-ASCII demo characters")
            cases.append({"text": text, "codepoints": [f"U+{ord(char):04X}" for char in text],
                          "marked_positions_zero_based": marked, "glyphs": names})
    label = ImageFont.truetype(str(source_path), 27)
    title = ImageFont.truetype(str(source_path), 40)
    small = ImageFont.truetype(str(source_path), 23)
    source_text = ImageFont.truetype(str(source_path), 140, layout_engine=ImageFont.Layout.BASIC)
    marked_text = ImageFont.truetype(str(font_path), 140, layout_engine=ImageFont.Layout.BASIC)
    canvas = Image.new("RGB", (1600, 800), "#f5f7fa")
    draw = ImageDraw.Draw(canvas)
    draw.text((56, 24), "amazon と、見た目が似た非ASCII文字列", font=title, fill="#172033")
    draw.rounded_rectangle((48, 111, 775, 648), radius=18, fill="white")
    draw.rounded_rectangle((825, 111, 1552, 648), radius=18, fill="#eaf2ff")
    draw.text((80, 126), "元の Noto Sans CJK JP", font=label, fill="#475569")
    draw.text((857, 126), "Noto Bene Regular — 実際のフォント描画", font=label, fill="#174b94")
    for text, y, caption in [
        (ASCII_TEXT, 221, "ASCII の a（U+0061）だけを使用"),
        (CONFUSABLE_TEXT, 447, "2つの a をキリル文字 а（U+0430）に置換"),
    ]:
        for x in (80, 857):
            draw.text((x, y - 45), caption, font=small, fill="#475569")
        # Draw the exact same Unicode string with each real font. No circles,
        # exclamation marks, or other badge graphics are overlaid by this script.
        draw.text((80, y), text, font=source_text, fill="#172033")
        draw.text((857, y), text, font=marked_text, fill="#172033")
    draw.text((56, 674), "ASCII : U+0061  U+006D  U+0061  U+007A  U+006F  U+006E", font=small, fill="#475569")
    draw.text((56, 714), "混在   : U+0430  U+006D  U+0430  U+007A  U+006F  U+006E", font=small, fill="#174b94")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    report = {"cases": cases, "renderer": "Pillow / FreeType, basic layout",
              "badge_overlay": False, "image": str(output_path)}
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = render_demo(args.font, args.source, args.output, args.report)
    except (OSError, ValueError) as exc:
        print(f"render_demo: {exc}", file=sys.stderr)
        return 1
    for case in report["cases"]:
        print(f"{case['text']}: {case['codepoints']}; marked positions {case['marked_positions_zero_based']}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
