"""Geometry regressions for preserving source ink while adding a warning badge."""

import math
import unittest

import pathops

from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.reverseContourPen import ReverseContourPen
from fontTools.pens.transformPen import TransformPen

from noto_bene.geometry import (
    badge_geometry,
    glyph_path,
    make_marked_glyph,
    marked_path,
)


class SyntheticGlyph:
    def __init__(self, *contours):
        self.contours = contours

    def draw(self, pen):
        for contour in self.contours:
            contour.replay(pen)


class ReflectedGlyph:
    def __init__(self, glyph):
        self.glyph = glyph

    def draw(self, pen):
        self.glyph.draw(TransformPen(pen, (-1, 0, 0, 1, 100, 0)))


class CompositeGlyph:
    def __init__(self, base):
        self.glyphSet = {"base": base}

    def draw(self, pen):
        pen.addComponent("base", (-1, 0, 0, 1, 100, 0))
        pen.addComponent("base", (1, 0, 0, 1, 200, 0))


def rectangle(x0, y0, x1, y1, clockwise=False):
    recording = RecordingPen()
    pen = ReverseContourPen(recording) if clockwise else recording
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()
    return recording


def rounded_contour(cx, cy, radius, cubic=False, clockwise=False):
    recording = RecordingPen()
    pen = ReverseContourPen(recording) if clockwise else recording
    pen.moveTo((cx + radius, cy))
    if cubic:
        k = radius * 0.5522847498307936
        pen.curveTo((cx + radius, cy + k), (cx + k, cy + radius), (cx, cy + radius))
        pen.curveTo((cx - k, cy + radius), (cx - radius, cy + k), (cx - radius, cy))
        pen.curveTo((cx - radius, cy - k), (cx - k, cy - radius), (cx, cy - radius))
        pen.curveTo((cx + k, cy - radius), (cx + radius, cy - k), (cx + radius, cy))
    else:
        pen.qCurveTo((cx + radius, cy + radius), (cx, cy + radius))
        pen.qCurveTo((cx - radius, cy + radius), (cx - radius, cy))
        pen.qCurveTo((cx - radius, cy - radius), (cx, cy - radius))
        pen.qCurveTo((cx + radius, cy - radius), (cx + radius, cy))
    pen.closePath()
    return recording


def result_path(glyph, upem=1000, advance=1000):
    result = make_marked_glyph(glyph, upem, advance)
    path = pathops.Path()
    result.draw(path.getPen(), None)
    return result, path


class BadgeGeometryTests(unittest.TestCase):
    def assert_closed(self, glyph):
        recording = RecordingPen()
        glyph.draw(recording, None)
        operations = [op for op, _ in recording.value]
        self.assertNotIn("endPath", operations)
        self.assertEqual(operations.count("moveTo"), operations.count("closePath"))
        self.assertGreater(operations.count("closePath"), 0)

    def assert_unchanged_outside_badge(self, glyph, advance=1000):
        source = glyph_path(glyph)
        spec = badge_geometry(source, 1000, advance)
        result, path = result_path(glyph, advance=advance)
        self.assert_closed(result)
        checked = 0
        for x in range(-47, 1100, 50):
            for y in range(-43, 1100, 50):
                distance = math.hypot(x - spec["cx"], y - spec["cy"])
                if distance <= spec["radius"] + spec["clearance"] + 3:
                    continue
                source_ink = source.contains((x, y))
                # Cu2Qu permits one font unit of error plus integer rounding.
                # Sample only points safely away from the source boundary.
                if any(source.contains((x + dx, y + dy)) != source_ink
                       for dx in (-2, 0, 2) for dy in (-2, 0, 2)):
                    continue
                self.assertEqual(
                    source_ink, path.contains((x, y)),
                    f"Source ink changed away from badge at {(x, y)}",
                )
                checked += 1
        self.assertGreater(checked, 100)

    def test_blank_glyph_becomes_circle_and_exclamation_only(self):
        glyph = SyntheticGlyph()
        spec = badge_geometry(glyph_path(glyph), 1000, 1000)
        result, path = result_path(glyph)
        cx, cy, radius = spec["cx"], spec["cy"], spec["radius"]
        # Probe ring, stem and dot separately, including the gaps between them.
        for x, y, ink in (
            (0.92, 0, True), (0.5, 0, False), (0, 0.35, True),
            (0, -0.5, True), (0, -0.25, False), (0, 0.75, False),
            (1.1, 0, False),
        ):
            with self.subTest(x=x, y=y):
                self.assertEqual(path.contains((cx + x * radius, cy + y * radius)), ink)
        self.assertEqual(result.numberOfContours, 4)
        self.assert_closed(result)

    def test_crossing_stroke_is_cut_at_clearance_and_contours_stay_closed(self):
        glyph = SyntheticGlyph(rectangle(0, 570, 1000, 630))
        spec = badge_geometry(glyph_path(glyph), 1000, 1000)
        result, path = result_path(glyph)
        cx, cy = spec["cx"], spec["cy"]
        self.assertTrue(glyph_path(glyph).contains((cx - spec["radius"], cy)))
        # Source stroke stops before the ring; the ring itself remains painted.
        self.assertFalse(path.contains((cx - spec["radius"] - spec["clearance"] / 2, cy)))
        self.assertTrue(path.contains((cx - spec["radius"] + spec["stroke"] / 2, cy)))
        self.assertFalse(path.contains((cx - spec["radius"] / 2, cy)))
        self.assertTrue(path.contains((200, 600)))
        self.assert_closed(result)

    def test_original_ink_outside_badge_is_preserved(self):
        glyph = SyntheticGlyph(
            rectangle(0, 0, 1000, 1000),
            rectangle(100, 100, 600, 600, clockwise=True),
        )
        self.assert_unchanged_outside_badge(glyph)

    def test_original_counter_away_from_badge_stays_open(self):
        glyph = SyntheticGlyph(
            rectangle(0, 0, 1000, 1000),
            rectangle(100, 100, 600, 600, clockwise=True),
        )
        _, path = result_path(glyph)
        self.assertFalse(path.contains((300, 300)))
        self.assertTrue(path.contains((50, 50)))
        self.assertTrue(path.contains((700, 300)))

    def test_source_winding_does_not_change_rendered_result(self):
        original = SyntheticGlyph(
            rectangle(0, 0, 1000, 1000),
            rectangle(100, 100, 600, 600, clockwise=True),
        )
        reversed_glyph = SyntheticGlyph(
            rectangle(0, 0, 1000, 1000, clockwise=True),
            rectangle(100, 100, 600, 600),
        )
        difference = pathops.op(
            marked_path(original, 1000, 1000),
            marked_path(reversed_glyph, 1000, 1000),
            pathops.PathOp.XOR,
        )
        self.assertAlmostEqual(difference.area, 0, places=3)

    def test_quadratic_and_cubic_curves_preserve_ink_away_from_badge(self):
        for cubic in (False, True):
            with self.subTest(cubic=cubic):
                glyph = SyntheticGlyph(
                    rounded_contour(500, 500, 450, cubic=cubic),
                    rounded_contour(500, 500, 200, cubic=cubic, clockwise=True),
                )
                self.assert_unchanged_outside_badge(glyph)
                _, path = result_path(glyph)
                self.assertFalse(path.contains((500, 500)))

    def test_components_and_reflection_preserve_source_ink(self):
        glyph = SyntheticGlyph(
            rectangle(0, 0, 100, 100), rectangle(25, 25, 75, 75, clockwise=True)
        )
        for transformed in (ReflectedGlyph(glyph), CompositeGlyph(glyph)):
            with self.subTest(kind=type(transformed).__name__):
                self.assert_unchanged_outside_badge(transformed)
                _, path = result_path(transformed)
                self.assertTrue(path.contains((10, 10)))
                self.assertFalse(path.contains((50, 50)))
                if isinstance(transformed, CompositeGlyph):
                    self.assertTrue(path.contains((210, 10)))
                    self.assertFalse(path.contains((250, 50)))

    def test_zero_and_narrow_advances_still_have_visible_closed_badges(self):
        for upem in (1000, 2048):
            for advance in (0, 1, upem // 2, upem):
                with self.subTest(upem=upem, advance=advance):
                    glyph = SyntheticGlyph()
                    result, path = result_path(glyph, upem=upem, advance=advance)
                    spec = badge_geometry(glyph_path(glyph), upem, advance)
                    self.assertEqual(spec["radius"] * 2, upem * 0.5)
                    self.assertEqual(path.bounds[2] - path.bounds[0], upem * 0.5)
                    self.assertEqual(path.bounds[3] - path.bounds[1], upem * 0.5)
                    self.assert_closed(result)
                    self.assertGreater(path.area, 0)
                    self.assertEqual(result.numberOfContours, 4)


if __name__ == "__main__":
    unittest.main()
