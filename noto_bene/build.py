"""Preserve Noto glyphs and visibly mark non-ASCII with a circled exclamation."""

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from .geometry import make_marked_glyph

# 2024-01-01 UTC. Fixed OpenType timestamp unless SOURCE_DATE_EPOCH is supplied.
DEFAULT_EPOCH = 1704067200
VERSION_PATH = Path(__file__).resolve().parents[1] / "sources" / "font-version.txt"


def load_font_version(path=None):
    """Read a canonical major.minor version that fits OpenType's signed 16.16."""
    path = VERSION_PATH if path is None else Path(path)
    version = path.read_text(encoding="utf-8").strip()
    if (not re.fullmatch(r"(?:0|[1-9][0-9]{0,4})\.[0-9]{3}", version)
            or int(version.split(".")[0]) > 32767):
        raise ValueError(f"Invalid font version in {path}: expected 0.000 through 32767.999")
    return version


def build_font(source, output, report=None):
    source, output = Path(source), Path(output)
    version = load_font_version()
    with TTFont(source, recalcTimestamp=False) as original:
        if "fvar" in original:
            raise ValueError("Use a static font; instantiate variable fonts first")
        glyph_set = original.getGlyphSet()
        source_cmap = original.getBestCmap()
        if not source_cmap or any(cp not in source_cmap for cp in range(32, 127)):
            raise ValueError("Source must contain every printable ASCII character")
        upem = original["head"].unitsPerEm
        metrics = {}
        glyphs = {}

        def original_glyph(name):
            recording = DecomposingRecordingPen(glyph_set)
            glyph_set[name].draw(recording)
            pen = TTGlyphPen(None)
            recording.replay(Cu2QuPen(pen, max_err=1.0,
                                    reverse_direction="CFF " in original))
            return pen.glyph()

        def marked(name, output_name):
            advance = original["hmtx"][name][0]
            try:
                glyph = make_marked_glyph(glyph_set[name], upem, advance)
            except Exception as exc:
                raise ValueError(f"Cannot mark {name}: {exc}") from exc
            glyph.recalcBounds(None)
            glyphs[output_name] = glyph
            # The half-em badge must fit narrow characters without colliding
            # with the next character. Combining marks retain zero advance.
            output_advance = max(advance, glyph.xMax + round(0.05 * upem)) if advance > 0 else 0
            metrics[output_name] = (output_advance, getattr(glyph, "xMin", 0))

        glyphs[".notdef"] = original_glyph(".notdef")
        metrics[".notdef"] = original["hmtx"][".notdef"]
        fallback = "fallback.nonascii"
        marked(".notdef", fallback)
        cmap = {cp: fallback for cp in range(128, 0x110000)
                if not 0xD800 <= cp <= 0xDFFF}
        counts = Counter()
        marked_names = {}
        for cp, source_name in sorted(source_cmap.items()):
            if not 0 <= cp <= 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                continue
            if cp < 128:
                name = f"ascii.{cp:04X}"
                glyphs[name] = original_glyph(source_name)
                metrics[name] = original["hmtx"][source_name]
                cmap[cp] = name
                counts["ascii"] += 1
                continue
            if source_name not in marked_names:
                name = f"marked.{original.getGlyphID(source_name):05d}"
                marked(source_name, name)
                marked_names[source_name] = name
            cmap[cp] = marked_names[source_name]
            counts["marked"] += 1
        counts["fallback"] = len(cmap) - sum(counts.values())
        if len(glyphs) > 65535:
            raise ValueError("Too many glyphs for TrueType (maximum 65535)")

        fb = FontBuilder(upem, isTTF=True)
        fb.setupGlyphOrder(list(glyphs))
        # Full scalar coverage exceeds the BMP format-4 capacity. Use the
        # standard full-repertoire Unicode and Windows format-12 encodings.
        fb.font["cmap"] = newTable("cmap")
        fb.font["cmap"].tableVersion = 0
        fb.font["cmap"].tables = []
        for platform, encoding in ((0, 4), (3, 10)):
            subtable = CmapSubtable.newSubtable(12)
            subtable.platformID, subtable.platEncID = platform, encoding
            subtable.language = 0
            subtable.cmap = cmap
            fb.font["cmap"].tables.append(subtable)
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics(metrics)
        hhea, os2 = original["hhea"], original["OS/2"]
        fb.setupHorizontalHeader(ascent=hhea.ascent, descent=hhea.descent,
                                 lineGap=hhea.lineGap)
        copyright_text = original["name"].getDebugName(0) or ""
        fb.setupNameTable({
            "familyName": "Noto Bene", "styleName": "Regular",
            "uniqueFontIdentifier": f"NotoBene-Regular-{version}",
            "fullName": "Noto Bene Regular", "psName": "NotoBene-Regular",
            "version": f"Version {version}", "copyright": copyright_text,
            "trademark": original["name"].getDebugName(7) or "",
            "licenseDescription": "Licensed under the SIL Open Font License, Version 1.1. See OFL.txt.",
            "licenseInfoURL": "https://openfontlicense.org/",
            "description": "Derived from Noto Sans CJK JP. ASCII preserved; non-ASCII retains its glyph with a circled exclamation at the upper right.",
        })
        all_ymax = max(getattr(glyph, "yMax", 0) for glyph in glyphs.values())
        all_ymin = min(getattr(glyph, "yMin", 0) for glyph in glyphs.values())
        fb.setupOS2(sTypoAscender=os2.sTypoAscender,
                    sTypoDescender=os2.sTypoDescender, sTypoLineGap=os2.sTypoLineGap,
                    usWinAscent=max(os2.usWinAscent, all_ymax),
                    usWinDescent=max(os2.usWinDescent, -all_ymin),
                    usWeightClass=400, usWidthClass=os2.usWidthClass,
                    fsType=0, fsSelection=0x40,
                    sxHeight=getattr(os2, "sxHeight", 0),
                    sCapHeight=getattr(os2, "sCapHeight", 0))
        fb.setupPost(keepGlyphNames=True)
        timestamp = int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH)) + 2082844800
        fb.font["head"].fontRevision = float(version)
        fb.font["head"].created = fb.font["head"].modified = timestamp
        fb.font.recalcTimestamp = False
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        try:
            fb.save(temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        summary = {
            "schema_version": 2, "family": "Noto Bene", "style": "Regular", "version": version,
            "source_filename": source.name,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "font_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "coverage": "All Unicode scalar values U+0080..U+10FFFF, plus source ASCII",
            "counts": dict(sorted(counts.items())), "glyph_count": len(glyphs),
            "mapped_codepoints": len(cmap), "units_per_em": upem,
            "operation": "original minus circular clearance disk, union circled exclamation badge",
            "marked_source_glyphs": len(marked_names),
            "empty_source_policy": "badge only; widen positive advance to fit",
            "missing_source_policy": "source .notdef with badge; widen positive advance to fit",
            "badge": {"diameter_em": 0.5, "radius_em": 0.25,
                      "minimum_right_em": 0.55, "advance_margin_em": 0.05,
                      "cell_right_ratio": 0.95,
                      "minimum_top_em": 0.85, "clearance_radius_ratio": 0.12,
                      "stroke_radius_ratio": 0.16},
            "cutout_control_points": "rounded to integer font units before boolean difference",
            "curve_error_units": 1.0,
            "source_date_epoch": timestamp - 2082844800,
        }
        if report is not None:
            report = Path(report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary
