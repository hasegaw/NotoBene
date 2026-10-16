#!/usr/bin/env python3
"""Render real ASCII/homoglyph strings with Noto and Noto Bene, and record code points."""

import argparse
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont

ASCII_TEXT = "amazon"


def render_demo(font_path, source_path, output_path, report_path=None, ascii_text=ASCII_TEXT):
    if not ascii_text or any(not 0x21 <= ord(char) <= 0x7E for char in ascii_text) or "a" not in ascii_text:
        raise ValueError("Demo text must contain printable non-space ASCII and at least one lowercase a")
    confusable_text = ascii_text.replace("a", "\u0430")
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
        for text in (ascii_text, confusable_text):
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
    # Keep one shared size so the two fonts remain directly comparable.
    for text_size in range(140, 23, -1):
        source_text = ImageFont.truetype(str(source_path), text_size, layout_engine=ImageFont.Layout.BASIC)
        marked_text = ImageFont.truetype(str(font_path), text_size, layout_engine=ImageFont.Layout.BASIC)
        if all(
            max(font.getlength(text), font.getbbox(text)[2]) <= 663
            and font.getbbox(text)[0] >= 0
            for font in (source_text, marked_text) for text in (ascii_text, confusable_text)
        ):
            break
    else:
        raise ValueError("Demo text is too long to fit legibly in the image")
    canvas = Image.new("RGB", (1600, 800), "#f5f7fa")
    draw = ImageDraw.Draw(canvas)
    draw.text((56, 24), "ASCII と、見た目が似たキリル文字の比較", font=title, fill="#172033")
    draw.rounded_rectangle((48, 111, 775, 648), radius=18, fill="white")
    draw.rounded_rectangle((825, 111, 1552, 648), radius=18, fill="#eaf2ff")
    draw.text((80, 126), "元の Noto Sans CJK JP", font=label, fill="#475569")
    draw.text((857, 126), "Noto Bene Regular — 実際のフォント描画", font=label, fill="#174b94")
    for text, y, caption in [
        (ascii_text, 221, "ASCII の a（U+0061）だけを使用"),
        (confusable_text, 447, f"{ascii_text.count('a')}つの a をキリル文字 а（U+0430）に置換"),
    ]:
        for x in (80, 857):
            draw.text((x, y - 45), caption, font=small, fill="#475569")
        # Draw the exact same Unicode string with each real font. No circles,
        # exclamation marks, or other badge graphics are overlaid by this script.
        draw.text((80, y), text, font=source_text, fill="#172033")
        draw.text((857, y), text, font=marked_text, fill="#172033")
    positions = "・".join(str(i + 1) for i in cases[1]["marked_positions_zero_based"])
    draw.text((56, 674), f"置換位置：{positions}文字目　a（U+0061）→ а（U+0430）", font=small, fill="#475569")
    draw.text((56, 714), "Noto Bene は非ASCIIの文字だけに警告マークを表示。マークもフォントの字形です。", font=small, fill="#174b94")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    report = {"cases": cases, "renderer": "Pillow / FreeType, basic layout",
              "badge_overlay": False, "font_size_px": text_size, "image": str(output_path)}
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
    parser.add_argument("--text", default=ASCII_TEXT, help="ASCII text whose lowercase a characters become Cyrillic (default: amazon)")
    args = parser.parse_args(argv)
    try:
        report = render_demo(args.font, args.source, args.output, args.report, args.text)
    except (OSError, ValueError) as exc:
        print(f"render_demo: {exc}", file=sys.stderr)
        return 1
    for case in report["cases"]:
        print(f"{case['text']}: {case['codepoints']}; marked positions {case['marked_positions_zero_based']}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
