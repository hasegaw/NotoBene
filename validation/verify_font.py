#!/usr/bin/env python3
"""Validate a Noto Bene TrueType font without importing the builder.

Copy this file anywhere and install fonttools and skia-pathops to run it.
Exit status: 0 = valid, 1 = failed checks, 2 = input/dependency error.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

try:
    import pathops
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    from fontTools.pens.roundingPen import RoundingPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.sfnt import calcChecksum
except ImportError as exc:
    print(f"Missing dependency: {exc}. Install fonttools and skia-pathops.", file=sys.stderr)
    raise SystemExit(2)

SCALAR_LIMIT = 0x110000
NON_ASCII_COUNT = SCALAR_LIMIT - 128 - 0x800
MARKED_NAME = re.compile(r"marked\.[0-9]{5}\Z")
FORBIDDEN_TABLES = {
    "CFF ", "CFF2", "GSUB", "GPOS", "GDEF", "kern", "morx", "mort", "kerx",
    "COLR", "CPAL", "SVG ", "CBDT", "CBLC", "sbix", "EBDT", "EBLC", "EBSC",
    "fvar", "gvar", "avar", "HVAR", "VVAR", "MVAR", "VARC", "MATH", "JSTF",
    "fpgm", "prep", "cvt ",
}
PATH_OPTIONS = dict(fix_winding=True, keep_starting_points=False, clockwise=True)


class Checks:
    """Keep diagnostics bounded even when an entire Unicode range is broken."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.error_count = 0

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.error_count += 1
            if len(self.errors) < 100:
                self.errors.append(message)


def is_scalar(cp: int) -> bool:
    return 0 <= cp < SCALAR_LIMIT and not 0xD800 <= cp <= 0xDFFF


def expected_plain(glyph: Any, glyph_set: Any, cubic: bool) -> Any:
    recording = DecomposingRecordingPen(glyph_set)
    glyph.draw(recording)
    pen = TTGlyphPen(None)
    recording.replay(Cu2QuPen(pen, max_err=1.0, reverse_direction=cubic))
    return pen.glyph()


def circle(cx: float, cy: float, radius: float) -> Any:
    """Four counterclockwise cubic arcs, beginning at the rightmost point."""
    k = radius * 0.5522847498307936
    path = pathops.Path()
    pen = path.getPen()
    pen.moveTo((cx + radius, cy))
    pen.curveTo((cx + radius, cy + k), (cx + k, cy + radius), (cx, cy + radius))
    pen.curveTo((cx - k, cy + radius), (cx - radius, cy + k), (cx - radius, cy))
    pen.curveTo((cx - radius, cy - k), (cx - k, cy - radius), (cx, cy - radius))
    pen.curveTo((cx + k, cy - radius), (cx + radius, cy - k), (cx + radius, cy))
    pen.closePath()
    return path


def badge_radius(upem: int, advance: int) -> float:
    return 0.25 * upem


def badge_path(cx: float, cy: float, radius: float) -> Any:
    ring = pathops.op(circle(cx, cy, radius), circle(cx, cy, radius - 0.16 * radius),
                      pathops.PathOp.DIFFERENCE, **PATH_OPTIONS)
    stem = pathops.Path()
    pen = stem.getPen()
    pen.moveTo((cx - 0.15 * radius, cy + 0.58 * radius))
    pen.lineTo((cx + 0.15 * radius, cy + 0.58 * radius))
    pen.lineTo((cx + 0.11 * radius, cy - 0.12 * radius))
    pen.lineTo((cx - 0.11 * radius, cy - 0.12 * radius))
    pen.closePath()
    ring.addPath(stem)
    ring.addPath(circle(cx, cy - 0.5 * radius, 0.15 * radius))
    return pathops.simplify(ring, **PATH_OPTIONS)


def path_to_glyph(path: Any) -> Any:
    pen = TTGlyphPen(None)
    path.draw(Cu2QuPen(pen, max_err=1.0, reverse_direction=False))
    return pen.glyph()


def expected_marked(glyph: Any, glyph_set: Any, upem: int, advance: int) -> Any:
    """Reconstruct the documented cutout and badge without builder imports."""
    recording = DecomposingRecordingPen(glyph_set)
    glyph.draw(recording)
    original = pathops.Path()
    recording.replay(original.getPen())
    has_outline = bool(list(original.verbs))
    _, _, xmax, ymax = original.bounds
    radius = badge_radius(upem, advance)
    right = max(xmax, 0.95 * advance, 0.55 * upem) if advance > 0 else (xmax if has_outline else 0.95 * upem)
    top = max(ymax, 0.85 * upem)
    cx, cy = right - radius, top - radius
    # Integer control points avoid unstable circle/cubic intersections in Skia.
    clearance = pathops.Path()
    circle(cx, cy, radius + 0.12 * radius).draw(RoundingPen(clearance.getPen()))
    clipped = pathops.op(original, clearance, pathops.PathOp.DIFFERENCE, **PATH_OPTIONS)
    clipped.addPath(badge_path(cx, cy, radius))
    return path_to_glyph(pathops.simplify(clipped, **PATH_OPTIONS))


def glyph_contours(glyph: Any, glyf: Any = None) -> list[tuple[list[tuple[float, float]], list[int]]]:
    coords, ends, flags = glyph.getCoordinates(glyf)
    result = []
    start = 0
    for end in ends:
        result.append((list(coords[start:end + 1]), [flag & 1 for flag in flags[start:end + 1]]))
        start = end + 1
    return result


def same_contour(expected: Any, actual: Any, dx: float = 0, dy: float = 0,
                 tolerance: float = 1) -> bool:
    """Compare contours while allowing a cyclic change of their starting point."""
    ep, ef = expected
    ap, af = actual
    if len(ep) != len(ap):
        return False
    if not ep:
        return True
    for offset in range(len(ap)):
        if all(ef[i] == af[(i + offset) % len(ap)]
               and abs(ex - (ap[(i + offset) % len(ap)][0] - dx)) <= tolerance
               and abs(ey - (ap[(i + offset) % len(ap)][1] - dy)) <= tolerance
               for i, (ex, ey) in enumerate(ep)):
            return True
    return False


def compare_outline(expected: Any, actual: Any, glyf: Any, label: str, checks: Checks) -> None:
    expected_contours = glyph_contours(expected)
    actual_contours = glyph_contours(actual, glyf)
    checks.require(len(expected_contours) == len(actual_contours), f"{label}: outline contour count differs from expected geometry")
    remaining = actual_contours[:]
    for contour in expected_contours:
        matching = next((i for i, candidate in enumerate(remaining) if same_contour(contour, candidate)), None)
        if matching is None:
            checks.require(False, f"{label}: outline differs from expected geometry by more than 1 unit")
            return
        remaining.pop(matching)


@lru_cache(maxsize=1024)
def marker_contours(radius: float) -> Any:
    return glyph_contours(path_to_glyph(badge_path(0, 0, radius)))


def has_badge(glyph: Any, glyf: Any, upem: int, advance: int) -> bool:
    """Find all four independent badge contours without knowing the source glyph."""
    contours = glyph_contours(glyph, glyf)
    radius = badge_radius(upem, advance)
    expected = marker_contours(radius)
    for points, _ in contours:
        if not points:
            continue
        xs, ys = zip(*points)
        if abs(max(xs) - min(xs) - 2 * radius) > 2 or abs(max(ys) - min(ys) - 2 * radius) > 2:
            continue
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        remaining = contours[:]
        for component in expected:
            match = next((i for i, candidate in enumerate(remaining)
                          if same_contour(component, candidate, cx, cy, tolerance=2)), None)
            if match is None:
                break
            remaining.pop(match)
        else:
            return True
    return False


def check_sfnt(font: TTFont, path: Path, checks: Checks) -> None:
    data = path.read_bytes()
    checks.require(data[:4] == b"\x00\x01\x00\x00", "Output is not a standalone TrueType sfnt")
    checks.require(calcChecksum(data) == 0xB1B0AFBA, "Whole-file TrueType checksum mismatch")
    for tag, entry in font.reader.tables.items():
        table = data[entry.offset:entry.offset + entry.length]
        checks.require(len(table) == entry.length, f"{tag}: table extends beyond the file")
        if tag == "head" and len(table) >= 12:
            table = table[:8] + b"\0\0\0\0" + table[12:]
        checks.require(calcChecksum(table) == entry.checkSum, f"{tag}: table checksum mismatch")
    required = {"head", "hhea", "maxp", "OS/2", "hmtx", "cmap", "loca", "glyf", "name", "post"}
    checks.require(required.issubset(font.keys()), f"Missing required tables: {sorted(required - set(font.keys()))}")
    checks.require(not FORBIDDEN_TABLES.intersection(font.keys()),
                   f"Unexpected substitution, positioning, color, variation, or hinting tables: {sorted(FORBIDDEN_TABLES.intersection(font.keys()))}")
    if not required.issubset(font.keys()):
        return
    order = font.getGlyphOrder()
    checks.require(len(order) == font["maxp"].numGlyphs, "maxp.numGlyphs mismatch")
    checks.require(len(order) == len(set(order)), "Duplicate glyph names")
    checks.require(bool(order) and order[0] == ".notdef", "Glyph 0 must be .notdef")
    checks.require(font["maxp"].tableVersion == 0x10000, "maxp is not the TrueType version")
    checks.require(16 <= font["head"].unitsPerEm <= 16384, "Invalid unitsPerEm")
    checks.require(set(font["hmtx"].metrics) == set(order), "hmtx does not cover the glyph order")
    offsets = list(font["loca"].locations)
    checks.require(len(offsets) == len(order) + 1, "loca entry count mismatch")
    checks.require(bool(offsets) and offsets[0] == 0, "loca does not begin at zero")
    checks.require(all(a <= b for a, b in zip(offsets, offsets[1:])), "loca offsets are not monotonic")
    checks.require(bool(offsets) and offsets[-1] <= font.reader.tables["glyf"].length,
                   "loca extends past the glyf table")
    for index, name in enumerate(order):
        glyph = font["glyf"][name]
        glyph.getCoordinates(font["glyf"])
        if glyph.numberOfContours >= 0 and index + 1 < len(offsets):
            # Read the unsigned instruction length from the original bytes:
            # fontTools skips zero-contour programs and parses other lengths as signed.
            base = font.reader.tables["glyf"].offset
            raw = data[base + offsets[index]:base + offsets[index + 1]]
            if glyph.numberOfContours == 0 and len(raw) in (0, 10):
                continue  # Empty data and a bare glyph header are both valid.
            instruction_offset = 10 + 2 * glyph.numberOfContours
            checks.require(len(raw) >= instruction_offset + 2,
                           f"{name}: malformed TrueType instruction length")
            if len(raw) >= instruction_offset + 2:
                checks.require(int.from_bytes(raw[instruction_offset:instruction_offset + 2], "big") == 0,
                               f"{name}: unexpected TrueType glyph instructions")



def verify(font_path: Path, source_path: Path | None = None) -> dict[str, Any]:
    """Check font structure, all Unicode mappings, and original-plus-badge geometry."""
    checks = Checks()
    summary: dict[str, Any] = {
        "font": str(font_path.resolve()),
        "source": str(source_path.resolve()) if source_path else None,
        "font_sha256": hashlib.sha256(font_path.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path else None,
        "source_checked": source_path is not None,
        "design": "original outlines with a circled exclamation badge on non-ASCII",
    }
    with TTFont(font_path, lazy=False) as font:
        check_sfnt(font, font_path, checks)
        if not {"glyf", "hmtx", "head", "cmap"}.issubset(font.keys()):
            return {**summary, "ok": False, "error_count": checks.error_count, "errors": checks.errors}
        order = set(font.getGlyphOrder())
        upem = font["head"].unitsPerEm
        cmap = font.getBestCmap() or {}
        summary.update(units_per_em=upem, glyph_count=len(order), cmap_count=len(cmap))
        full_maps = [table for table in font["cmap"].tables if table.isUnicode() and table.format == 12]
        checks.require(bool(full_maps), "No Unicode format 12 cmap")
        windows_bmp_maps = [table for table in font["cmap"].tables
                            if table.platformID == 3 and table.platEncID == 1]
        checks.require(len(windows_bmp_maps) == 1,
                       "Expected exactly one Windows BMP cmap (platform 3, encoding 1)")
        # Format 4 reserves U+FFFF for its terminal segment; format 12 keeps
        # that mapping and the supplementary planes in the full Unicode map.
        expected_bmp = {cp: name for cp, name in cmap.items() if cp < 0xFFFF}
        for table in windows_bmp_maps:
            checks.require(table.format == 4, "Windows BMP cmap must use format 4")
            checks.require(table.cmap == expected_bmp,
                           "Windows BMP cmap coverage or mappings differ from the full Unicode cmap")
        for table in font["cmap"].tables:
            checks.require(table.format in (4, 12), f"Unexpected cmap format {table.format}")
            if table.isUnicode():
                for cp, name in table.cmap.items():
                    checks.require(is_scalar(cp), f"cmap contains non-scalar U+{cp:04X}")
                    checks.require(cmap.get(cp) == name, f"cmap subtables disagree at U+{cp:04X}")
                    checks.require(name in order, f"U+{cp:04X}: unmapped glyph {name}")
        for table in full_maps:
            checks.require(table.cmap == cmap, "Unicode format 12 tables disagree")
        non_ascii_count = sum(cp >= 128 and is_scalar(cp) for cp in cmap)
        summary["non_ascii_count"] = non_ascii_count
        checks.require(non_ascii_count == NON_ASCII_COUNT,
                       f"Expected {NON_ASCII_COUNT} non-ASCII scalar mappings, got {non_ascii_count}")
        marked_names = {name for name in order if MARKED_NAME.fullmatch(name) or name == "fallback.nonascii"}
        checks.require("fallback.nonascii" in marked_names, "Missing marked non-ASCII fallback glyph")
        for name in order:
            checks.require(name in marked_names or name == ".notdef" or re.fullmatch(r"ascii\.[0-9A-F]{4}", name) is not None,
                           f"Unexpected glyph name {name}")
            glyph = font["glyf"][name]
            checks.require(glyph.numberOfContours >= 0, f"{name}: expected decomposed TrueType outline")
            if name in marked_names:
                checks.require(glyph.numberOfContours >= 4, f"{name}: missing circled exclamation badge contours")
                if glyph.numberOfContours > 0:
                    advance, lsb = font["hmtx"][name]
                    checks.require(lsb == glyph.xMin, f"{name}: LSB differs from new outline bounds")
                    checks.require(advance == 0 or advance >= glyph.xMax + round(0.05 * upem),
                                   f"{name}: advance does not include badge spacing")
                # Always check the badge independently: reproducing the same
                # boolean operations can otherwise reproduce a geometry defect.
                checks.require(has_badge(glyph, font["glyf"], upem, font["hmtx"][name][0]),
                               f"{name}: circled exclamation badge is missing or damaged")
        for cp, name in cmap.items():
            if cp >= 128:
                checks.require(name in marked_names, f"U+{cp:04X}: non-ASCII maps to an unmarked glyph {name}")
            else:
                checks.require(name == f"ascii.{cp:04X}", f"U+{cp:04X}: unexpected ASCII glyph name {name}")
        summary["marked_glyphs"] = len(marked_names)
        summary["badge_checked"] = len(marked_names)

        if source_path:
            with TTFont(source_path, lazy=False) as source:
                source_cmap = source.getBestCmap() or {}
                source_glyphs = source.getGlyphSet()
                source_metrics = source["hmtx"].metrics
                checks.require(source["head"].unitsPerEm == upem, "Source and output unitsPerEm differ")
                expected_ascii_cps = {cp for cp in source_cmap if 0 <= cp < 128}
                actual_ascii_cps = {cp for cp in cmap if 0 <= cp < 128}
                checks.require(expected_ascii_cps == actual_ascii_cps, "ASCII cmap coverage differs from source")
                cubic = "CFF " in source or "CFF2" in source
                ascii_checked = 0
                for cp in sorted(expected_ascii_cps & actual_ascii_cps):
                    original_name, output_name = source_cmap[cp], cmap[cp]
                    if output_name not in order:
                        continue
                    checks.require(font["hmtx"][output_name] == source_metrics[original_name],
                                   f"U+{cp:04X}: ASCII advance/LSB differs from source")
                    expected = expected_plain(source_glyphs[original_name], source_glyphs, cubic)
                    compare_outline(expected, font["glyf"][output_name], font["glyf"], f"U+{cp:04X}: ASCII", checks)
                    ascii_checked += 1
                if ".notdef" in order:
                    expected = expected_plain(source_glyphs[".notdef"], source_glyphs, cubic)
                    compare_outline(expected, font["glyf"][".notdef"], font["glyf"], ".notdef", checks)
                    checks.require(font["hmtx"][".notdef"] == source_metrics[".notdef"], ".notdef metrics differ from source")
                marked_sources = {"fallback.nonascii": ".notdef"}
                source_supported = 0
                for cp, output_name in cmap.items():
                    if cp < 128 or not is_scalar(cp):
                        continue
                    original_name = source_cmap.get(cp)
                    if original_name is None:
                        expected_name = "fallback.nonascii"
                    else:
                        expected_name = f"marked.{source.getGlyphID(original_name):05d}"
                        marked_sources[expected_name] = original_name
                        source_supported += 1
                    checks.require(output_name == expected_name,
                                   f"U+{cp:04X}: expected marked glyph {expected_name}, got {output_name}")
                checks.require(marked_names == set(marked_sources), "Marked glyph inventory differs from source mappings")
                marked_checked = 0
                for output_name, original_name in marked_sources.items():
                    if output_name not in order:
                        continue
                    advance = source_metrics[original_name][0]
                    expected = expected_marked(source_glyphs[original_name], source_glyphs, upem, advance)
                    expected.recalcBounds(None)
                    expected_advance = max(advance, expected.xMax + round(0.05 * upem)) if advance > 0 else 0
                    checks.require(font["hmtx"][output_name][0] == expected_advance,
                                   f"{output_name}: advance differs from source/badge spacing policy")
                    compare_outline(expected, font["glyf"][output_name], font["glyf"], output_name, checks)
                    marked_checked += 1
                summary.update(ascii_checked=ascii_checked, source_non_ascii_mappings=source_supported,
                               source_marked_glyphs_checked=marked_checked)
    summary.update(ok=checks.error_count == 0, error_count=checks.error_count, errors=checks.errors)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", required=True, type=Path, help="Built Noto Bene .ttf file")
    parser.add_argument("--source", type=Path, help="Original Noto .otf/.ttf; enables original-outline and badge comparison")
    parser.add_argument("--json", dest="json_path", type=Path, help="Also write a machine-readable report")
    args = parser.parse_args(argv)
    try:
        report = verify(args.font, args.source)
    except Exception as exc:
        report = {"ok": False, "font": str(args.font), "source": str(args.source) if args.source else None,
                  "error_count": 1, "errors": [f"{type(exc).__name__}: {exc}"]}
        exit_code = 2
    else:
        exit_code = 0 if report["ok"] else 1
    if args.json_path:
        try:
            args.json_path.parent.mkdir(parents=True, exist_ok=True)
            args.json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Cannot write JSON report: {exc}", file=sys.stderr)
            return 2
    if report["ok"]:
        coverage = "source comparison included" if args.source else "source comparison skipped (no --source)"
        print(f"PASS: {report['glyph_count']} glyphs, {report['non_ascii_count']} non-ASCII scalars; {coverage}")
    else:
        print(f"FAIL: {report['error_count']} error(s)", file=sys.stderr)
        for message in report["errors"]:
            print(f"  {message}", file=sys.stderr)
        if report["error_count"] > len(report["errors"]):
            print("  Further diagnostics omitted; see error_count.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
