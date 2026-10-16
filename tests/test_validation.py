"""Make valid fonts internally, then ensure the standalone validator finds damage."""

import hashlib
import struct
from pathlib import Path
import tempfile
import unittest

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.DefaultTable import DefaultTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from fontTools.ttLib.tables.ttProgram import Program

from noto_bene.build import build_font
from noto_bene.cmap import prepare_windows_bmp_cmap
from validation.verify_font import verify
from test_build import make_source_font


class ValidationRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="noto-bene-validator-tests-")
        cls.root = Path(cls.directory.name)
        cls.source = cls.root / "source.ttf"
        cls.valid = cls.root / "valid.ttf"
        make_source_font(cls.source)
        build_font(cls.source, cls.valid)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def change_font(self, filename, mutation):
        output = self.root / filename
        with TTFont(self.valid) as font:
            mutation(font)
            prepare_windows_bmp_cmap(font)
            font.save(output)
        return output

    def test_valid_font_and_source_pass(self):
        report = verify(self.valid, self.source)
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["source_checked"])
        self.assertEqual(report["non_ascii_count"], 1111936)
        self.assertEqual(report["ascii_checked"], 97)
        self.assertEqual(report["font_sha256"], hashlib.sha256(self.valid.read_bytes()).hexdigest())
        self.assertEqual(report["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_valid_font_without_source_passes_badge_detection(self):
        report = verify(self.valid)
        self.assertTrue(report["ok"], report["errors"])
        self.assertFalse(report["source_checked"])
        self.assertEqual(report["font_sha256"], hashlib.sha256(self.valid.read_bytes()).hexdigest())
        self.assertIsNone(report["source_sha256"])

    def test_rejects_incomplete_unicode_coverage(self):
        def remove_mapping(font):
            # All Unicode subtables must be modified to isolate coverage checking.
            for table in font["cmap"].tables:
                table.cmap.pop(0x10FFFF, None)
        damaged = self.change_font("missing.ttf", remove_mapping)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertTrue(any("non-ASCII scalar mappings" in error for error in report["errors"]))

    def test_rejects_missing_windows_bmp_cmap(self):
        def remove_windows_bmp(font):
            font["cmap"].tables = [table for table in font["cmap"].tables
                                   if (table.platformID, table.platEncID) != (3, 1)]
        damaged = self.change_font("missing-windows-bmp.ttf", remove_windows_bmp)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertIn("Expected exactly one Windows BMP cmap (platform 3, encoding 1)",
                      report["errors"])

    def test_rejects_incomplete_windows_bmp_cmap(self):
        def remove_bmp_mapping(font):
            for table in font["cmap"].tables:
                if (table.platformID, table.platEncID) == (3, 1):
                    del table.cmap[0x3000]
        damaged = self.change_font("incomplete-windows-bmp.ttf", remove_bmp_mapping)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertIn("Windows BMP cmap coverage or mappings differ from the full Unicode cmap",
                      report["errors"])

    def test_rejects_wrong_windows_bmp_cmap_format(self):
        def replace_bmp_format(font):
            for index, table in enumerate(font["cmap"].tables):
                if (table.platformID, table.platEncID) == (3, 1):
                    replacement = CmapSubtable.newSubtable(12)
                    replacement.platformID, replacement.platEncID = 3, 1
                    replacement.language = table.language
                    replacement.cmap = table.cmap.copy()
                    font["cmap"].tables[index] = replacement
        damaged = self.change_font("wrong-windows-bmp-format.ttf", replace_bmp_format)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertIn("Windows BMP cmap must use format 4", report["errors"])

    def test_rejects_wrong_source_glyph_mapping(self):
        def replace_mapping(font):
            wrong = font.getBestCmap()[0x80]
            for table in font["cmap"].tables:
                table.cmap[0x81] = wrong
        damaged = self.change_font("wrong-style.ttf", replace_mapping)
        report = verify(damaged, self.source)
        self.assertFalse(report["ok"])
        self.assertTrue(any("U+0081: expected marked glyph" in error for error in report["errors"]))

    def test_rejects_altered_ascii_outline(self):
        def alter_ascii(font):
            glyph = font["glyf"]["ascii.0041"]
            x, y = glyph.coordinates[0]
            glyph.coordinates[0] = (x + 10, y)
        damaged = self.change_font("wrong-ascii.ttf", alter_ascii)
        report = verify(damaged, self.source)
        self.assertFalse(report["ok"])
        self.assertTrue(any("U+0041: ASCII: outline differs" in error for error in report["errors"]))

    def test_rejects_damaged_badge_without_source(self):
        def alter_badge(font):
            glyph = font["glyf"][font.getBestCmap()[0x3000]]
            x, y = glyph.coordinates[0]
            glyph.coordinates[0] = (x + 10, y)
        damaged = self.change_font("wrong-badge.ttf", alter_badge)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertTrue(any("badge is missing or damaged" in error for error in report["errors"]))
        # Source reconstruction must not disable the independent badge check.
        report_with_source = verify(damaged, self.source)
        self.assertFalse(report_with_source["ok"])
        self.assertTrue(any("badge is missing or damaged" in error for error in report_with_source["errors"]))

    def test_rejects_narrowed_advance(self):
        def narrow_advance(font):
            name = font.getBestCmap()[0x80]
            advance, lsb = font["hmtx"][name]
            font["hmtx"][name] = (advance - 20, lsb)
        damaged = self.change_font("wrong-advance.ttf", narrow_advance)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertTrue(any("advance does not include badge spacing" in error for error in report["errors"]))

    def replace_glyph_data(self, filename, name, data):
        """Keep raw empty-glyph data that fontTools normally discards on save."""
        output = self.root / filename
        with TTFont(self.valid, recalcBBoxes=False) as font:
            index = font.getGlyphID(name)
            offsets = list(font["loca"].locations)
            start, end = offsets[index:index + 2]
            original = font.reader["glyf"]
            table = DefaultTable("glyf")
            table.data = original[:start] + data + original[end:]
            font["glyf"] = table
            delta = len(data) - (end - start)
            font["loca"].set([offset + (delta if i > index else 0)
                              for i, offset in enumerate(offsets)])
            prepare_windows_bmp_cmap(font)
            font.save(output)
        return output

    def test_rejects_hinting_tables(self):
        for tag in ("fpgm", "prep", "cvt "):
            def add_table(font):
                table = DefaultTable(tag)
                table.data = b""
                font[tag] = table
            damaged = self.change_font(f"hint-table-{tag.strip()}.ttf", add_table)
            for source in (None, self.source):
                with self.subTest(table=tag, source=source):
                    report = verify(damaged, source)
                    self.assertFalse(report["ok"])
                    self.assertTrue(any("hinting tables" in error and tag in error
                                        for error in report["errors"]))

    def test_rejects_instructions_in_every_glyph_category(self):
        names = []
        def add_instructions(font):
            names.extend((font.getBestCmap()[0x80], "fallback.nonascii", "ascii.0041", ".notdef"))
            for name in names:
                program = Program()
                program.fromBytecode([0])  # SVTCA[y]; no rendering is performed.
                font["glyf"][name].program = program
            # Validation must inspect glyph data, regardless of this metadata.
            font["maxp"].maxSizeOfInstructions = 0
        damaged = self.change_font("glyph-instructions.ttf", add_instructions)
        for source in (None, self.source):
            with self.subTest(source=source):
                report = verify(damaged, source)
                self.assertFalse(report["ok"])
                for name in names:
                    self.assertIn(f"{name}: unexpected TrueType glyph instructions", report["errors"])

    def test_rejects_instructions_in_zero_contour_glyph(self):
        data = struct.pack(">hhhhhH", 0, 0, 0, 0, 0, 1) + bytes([0])
        damaged = self.replace_glyph_data("empty-instructions.ttf", "ascii.0020", data)
        with TTFont(damaged) as font:
            self.assertFalse(hasattr(font["glyf"]["ascii.0020"], "program"))
        for source in (None, self.source):
            with self.subTest(source=source):
                report = verify(damaged, source)
                self.assertFalse(report["ok"])
                self.assertIn("ascii.0020: unexpected TrueType glyph instructions", report["errors"])

    def test_accepts_zero_contour_glyph_without_instructions(self):
        header = struct.pack(">hhhhh", 0, 0, 0, 0, 0)
        for suffix in (b"", bytes(2)):
            with self.subTest(instruction_length_present=bool(suffix)):
                valid = self.replace_glyph_data("empty-without-instructions.ttf", "ascii.0020", header + suffix)
                report = verify(valid, self.source)
                self.assertTrue(report["ok"], report["errors"])

    def test_rejects_nonstandard_negative_contour_count(self):
        # Some parsers treat only -1 as composite; all negative counts must fail.
        data = struct.pack(">hhhhhHHbbH", -2, 0, 0, 0, 0, 0x0102, 0, 0, 0, 2) + bytes((0, 1))
        damaged = self.replace_glyph_data("negative-contours.ttf", "ascii.0020", data)
        report = verify(damaged, self.source)
        self.assertFalse(report["ok"])
        self.assertIn("ascii.0020: expected decomposed TrueType outline", report["errors"])

    def test_rejects_truncated_zero_contour_instruction_length(self):
        data = struct.pack(">hhhhh", 0, 0, 0, 0, 0) + bytes(1)
        damaged = self.replace_glyph_data("empty-truncated.ttf", "ascii.0020", data)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertIn("ascii.0020: malformed TrueType instruction length", report["errors"])

    def test_rejects_stale_checksum(self):
        with TTFont(self.valid) as font:
            flag_offset = font.reader.tables["head"].offset + 17
        data = bytearray(self.valid.read_bytes())
        data[flag_offset] ^= 1
        damaged = self.root / "checksum.ttf"
        damaged.write_bytes(data)
        report = verify(damaged)
        self.assertFalse(report["ok"])
        self.assertTrue(any("checksum mismatch" in error for error in report["errors"]))
        self.assertEqual(report["font_sha256"], hashlib.sha256(data).hexdigest())
        self.assertNotEqual(report["font_sha256"], hashlib.sha256(self.valid.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
