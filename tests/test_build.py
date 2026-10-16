"""End-to-end font generation tests using an offline synthetic TrueType source."""

import hashlib
import json
import os

import pathops
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from noto_bene.build import build_font, load_font_version


def outline(*contours):
    pen = TTGlyphPen(None)
    for points in contours:
        pen.moveTo(points[0])
        for point in points[1:]:
            pen.lineTo(point)
        pen.closePath()
    return pen.glyph()


def make_source_font(path):
    """Write a tiny fixture with aliases, counters, blank glyphs and substitutions."""
    triangle = ((10, 0), (250, 700), (490, 0))
    outside = ((10, 0), (10, 700), (590, 700), (590, 0))
    inside = ((100, 100), (500, 100), (500, 600), (100, 600))
    glyphs = {
        ".notdef": outline(((0, 0), (400, 700), (800, 0))),
        "latin": outline(triangle),
        "space": outline(),
        "hole": outline(outside, inside),
        "mark": outline(triangle),
        "wideblank": outline(),
        "supplementary": outline(outside, inside),
    }
    widths = {
        ".notdef": (1000, 0), "latin": (530, 10), "space": (250, 0),
        "hole": (630, 10), "mark": (0, 10), "wideblank": (1000, 0),
        "supplementary": (1200, 10),
    }
    cmap = {cp: "latin" for cp in range(33, 127)}
    cmap.update({
        0: "space", 32: "space", 127: "latin", 128: "latin", 129: "hole",
        130: "mark", 0x3000: "wideblank", 0xD800: "latin", 0x1F600: "supplementary",
    })
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(list(glyphs))
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(widths)
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({
        "familyName": "Synthetic Source", "styleName": "Regular",
        "uniqueFontIdentifier": "SyntheticSource-Regular-1",
        "fullName": "Synthetic Source Regular", "psName": "SyntheticSource-Regular",
        "version": "Version 1.000",
        "copyright": "Copyright 2026 Synthetic Source Authors.",
        "trademark": "Synthetic Source is a trademark of Synthetic Source Authors.",
    })
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    addOpenTypeFeaturesFromString(fb.font, "feature liga { sub latin latin by hole; } liga;")
    fb.font["head"].created = fb.font["head"].modified = 3786912000
    fb.font.recalcTimestamp = False
    fb.save(path)


def recorded_outline(font, name):
    pen = RecordingPen()
    font.getGlyphSet()[name].draw(pen)
    return pen.value


class BuildIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="noto-bene-tests-")
        cls.addClassCleanup(cls.workspace.cleanup)
        directory = Path(cls.workspace.name)
        cls.source_path = directory / "source.ttf"
        cls.output_path = directory / "NotoBene-Regular.ttf"
        cls.second_path = directory / "NotoBene-Regular-again.ttf"
        cls.report_path = directory / "report.json"
        make_source_font(cls.source_path)
        with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1704067200"}):
            cls.summary = build_font(cls.source_path, cls.output_path, cls.report_path)
            build_font(cls.source_path, cls.second_path)
        cls.source = TTFont(cls.source_path, recalcTimestamp=False)
        cls.addClassCleanup(cls.source.close)
        cls.font = TTFont(cls.output_path, recalcTimestamp=False)
        cls.addClassCleanup(cls.font.close)
        cls.source_cmap = cls.source.getBestCmap()
        cls.cmap = cls.font.getBestCmap()

    def test_legal_notices_survive_renaming_and_serialization(self):
        for name_id in (0, 7):
            expected = self.source["name"].getDebugName(name_id)
            self.assertTrue(expected)
            records = [record for record in self.font["name"].names
                       if record.nameID == name_id]
            self.assertEqual({record.platformID for record in records}, {1, 3})
            for record in records:
                with self.subTest(name_id=name_id, platform=record.platformID):
                    self.assertEqual(record.toUnicode(), expected)
        for name_id in (1, 6):
            self.assertNotEqual(self.font["name"].getDebugName(name_id),
                                self.source["name"].getDebugName(name_id))

    def test_ascii_outlines_and_metrics_are_preserved(self):
        for cp in (cp for cp in self.source_cmap if cp < 128):
            with self.subTest(codepoint=hex(cp)):
                original_name, output_name = self.source_cmap[cp], self.cmap[cp]
                self.assertEqual(
                    recorded_outline(self.source, original_name),
                    recorded_outline(self.font, output_name),
                )
                self.assertEqual(
                    self.source["hmtx"][original_name], self.font["hmtx"][output_name]
                )

    def test_ascii_alias_is_split_and_non_ascii_is_marked_with_sufficient_advance(self):
        self.assertEqual(self.source_cmap[65], self.source_cmap[128])
        self.assertNotEqual(self.cmap[65], self.cmap[128])
        self.assertNotEqual(
            recorded_outline(self.font, self.cmap[65]),
            recorded_outline(self.font, self.cmap[128]),
        )
        for cp in (128, 129, 130, 0x3000, 0x1F600):
            with self.subTest(codepoint=hex(cp)):
                original_name, output_name = self.source_cmap[cp], self.cmap[cp]
                self.assertTrue(output_name.startswith("marked."))
                self.assertNotEqual(
                    recorded_outline(self.source, original_name),
                    recorded_outline(self.font, output_name),
                )
                source_advance = self.source["hmtx"][original_name][0]
                output_advance = self.font["hmtx"][output_name][0]
                if source_advance == 0:
                    self.assertEqual(output_advance, 0)
                else:
                    self.assertGreaterEqual(output_advance, source_advance)
                    self.assertGreaterEqual(
                        output_advance, self.font["glyf"][output_name].xMax + 50
                    )
                self.assertEqual(
                    self.font["hmtx"][output_name][1],
                    self.font["glyf"][output_name].xMin,
                )

    def test_narrow_non_ascii_grows_to_fit_half_em_badge_without_changing_ascii(self):
        self.assertEqual(self.source["hmtx"][self.source_cmap[128]][0], 530)
        self.assertEqual(self.font["hmtx"][self.cmap[65]][0], 530)
        self.assertGreaterEqual(self.font["hmtx"][self.cmap[128]][0], 600)
        self.assertEqual(self.font["hmtx"][self.cmap[130]][0], 0)

    def test_blank_becomes_badge_and_source_counter_remains_open(self):
        self.assertEqual(self.source["glyf"][self.source_cmap[0x3000]].numberOfContours, 0)
        self.assertGreater(self.font["glyf"][self.cmap[0x3000]].numberOfContours, 0)
        marked = pathops.Path()
        self.font.getGlyphSet()[self.cmap[129]].draw(marked.getPen())
        self.assertTrue(marked.contains((40, 40)))
        self.assertFalse(marked.contains((300, 300)))

    def test_all_non_ascii_scalars_are_covered_without_surrogates(self):
        non_ascii = [cp for cp in self.cmap if cp >= 128]
        self.assertEqual(len(non_ascii), 0x110000 - 128 - 0x800)
        self.assertTrue(all(cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF for cp in non_ascii))
        self.assertIn(0xD800, self.source_cmap)
        self.assertNotIn(0xD800, self.cmap)
        self.assertNotIn(0xDFFF, self.cmap)
        for cp in (128, 0xD7FF, 0xE000, 0xFFFF, 0x10000, 0x10FFFF):
            self.assertIn(cp, self.cmap)
        for table in self.font["cmap"].tables:
            if table.format == 12:
                self.assertEqual(table.cmap, self.cmap)

    def test_windows_bmp_map_matches_full_unicode_except_sentinel(self):
        self.assertEqual(
            [(table.platformID, table.platEncID, table.format)
             for table in self.font["cmap"].tables],
            [(0, 4, 12), (3, 1, 4), (3, 10, 12)],
        )
        bmp = self.font["cmap"].getcmap(3, 1)
        self.assertEqual(bmp.cmap, {cp: name for cp, name in self.cmap.items()
                                  if cp < 0xFFFF})
        for cp in (0, 32, 128, 131, 0x3000, 0xD7FF, 0xE000, 0xFFFE):
            self.assertEqual(bmp.cmap[cp], self.cmap[cp])
        self.assertNotIn(0xFFFF, bmp.cmap)
        self.assertIn(0xFFFF, self.cmap)
        self.assertIn(0x10000, self.cmap)

    def test_unknown_non_ascii_uses_marked_notdef_but_notdef_itself_is_unchanged(self):
        original_notdef = recorded_outline(self.source, ".notdef")
        self.assertEqual(recorded_outline(self.font, ".notdef"), original_notdef)
        self.assertEqual(self.font["hmtx"][".notdef"], self.source["hmtx"][".notdef"])
        for cp in (131, 0xFFFF, 0x1F601, 0x10FFFF):
            with self.subTest(codepoint=hex(cp)):
                self.assertNotIn(cp, self.source_cmap)
                name = self.cmap[cp]
                self.assertEqual(name, "fallback.nonascii")
                self.assertNotEqual(recorded_outline(self.font, name), original_notdef)
                self.assertGreaterEqual(
                    self.font["hmtx"][name][0], self.source["hmtx"][".notdef"][0]
                )
                self.assertGreaterEqual(
                    self.font["hmtx"][name][0], self.font["glyf"][name].xMax + 50
                )

    def test_source_substitution_tables_are_removed(self):
        self.assertIn("GSUB", self.source)
        self.assertNotIn("GSUB", self.font)
        self.assertNotIn("GPOS", self.font)
        self.assertNotIn("CFF ", self.font)
        self.assertIn("glyf", self.font)

    def test_repeated_builds_are_byte_identical_and_report_matches(self):
        first_hash = hashlib.sha256(self.output_path.read_bytes()).hexdigest()
        second_hash = hashlib.sha256(self.second_path.read_bytes()).hexdigest()
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(self.summary["font_sha256"], first_hash)
        self.assertEqual(json.loads(self.report_path.read_text()), self.summary)
        self.assertEqual(self.summary["mapped_codepoints"], len(self.cmap))

    def test_configured_version_updates_serialized_font_and_report(self):
        directory = Path(self.workspace.name)
        version_path = directory / "font-version.txt"
        version_path.write_text("3.125\n", encoding="utf-8")
        output_path = directory / "versioned.ttf"
        second_path = directory / "versioned-again.ttf"
        report_path = directory / "versioned.json"
        with patch("noto_bene.build.VERSION_PATH", version_path):
            result = build_font(self.source_path, output_path, report_path)
            build_font(self.source_path, second_path)
        self.assertEqual(result["version"], "3.125")
        self.assertEqual(json.loads(report_path.read_text())["version"], "3.125")
        self.assertEqual(output_path.read_bytes(), second_path.read_bytes())
        with TTFont(output_path, recalcTimestamp=False) as font:
            self.assertEqual(font["head"].fontRevision, 3.125)
            for record in font["name"].names:
                if record.nameID in (3, 5):
                    expected = {3: "NotoBene-Regular-3.125", 5: "Version 3.125"}
                    self.assertEqual(record.toUnicode(), expected[record.nameID])


class FontVersionTests(unittest.TestCase):
    def test_invalid_versions_are_rejected_before_build(self):
        with tempfile.TemporaryDirectory(prefix="font-version-tests-") as directory:
            path = Path(directory) / "font-version.txt"
            output = Path(directory) / "output.ttf"
            for version in ("", "2", "2.0", "2.0000", "02.000", "-1.000", "NaN",
                            "32768.000", "2.000\n3.000", "２.000"):
                with self.subTest(version=version):
                    path.write_text(version, encoding="utf-8")
                    with patch("noto_bene.build.VERSION_PATH", path):
                        with self.assertRaisesRegex(ValueError, "Invalid font version"):
                            build_font(Path(directory) / "missing.ttf", output)
                    self.assertFalse(output.exists())

    def test_supported_version_bounds(self):
        with tempfile.TemporaryDirectory(prefix="font-version-tests-") as directory:
            path = Path(directory) / "font-version.txt"
            for version in ("0.000", "2.000", "32767.999"):
                with self.subTest(version=version):
                    path.write_text(version + "\n", encoding="utf-8")
                    self.assertEqual(load_font_version(path), version)


if __name__ == "__main__":
    unittest.main()
