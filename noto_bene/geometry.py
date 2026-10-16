"""Cut a circular clearance out of a glyph and add a circled exclamation."""

import pathops
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.roundingPen import RoundingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

KAPPA = 0.5522847498307936
OPS = dict(fix_winding=True, keep_starting_points=False, clockwise=True)


def glyph_path(glyph):
    """Expand components, including transforms, without changing fill winding."""
    path = pathops.Path()
    glyph_set = getattr(glyph, "glyphSet", None)
    if glyph_set is not None:
        recording = DecomposingRecordingPen(glyph_set)
        glyph.draw(recording)
        recording.replay(path.getPen())
    else:
        glyph.draw(path.getPen())
    return path


def circle_path(cx, cy, radius):
    """Closed counterclockwise circle, approximated by four cubic curves."""
    r, k = radius, radius * KAPPA
    path = pathops.Path()
    pen = path.getPen()
    pen.moveTo((cx + r, cy))
    pen.curveTo((cx + r, cy + k), (cx + k, cy + r), (cx, cy + r))
    pen.curveTo((cx - k, cy + r), (cx - r, cy + k), (cx - r, cy))
    pen.curveTo((cx - r, cy - k), (cx - k, cy - r), (cx, cy - r))
    pen.curveTo((cx + k, cy - r), (cx + r, cy - k), (cx + r, cy))
    pen.closePath()
    return path


def badge_geometry(path, upem, advance):
    """Place a readable badge at the upper right of the glyph's ink/cell.

    The circle diameter is half an em. Positive-advance glyphs reserve room
    for its full diameter; zero-advance marks stay anchored to their ink.
    """
    has_outline = bool(list(path.verbs))
    _, _, xmax, ymax = path.bounds if has_outline else (0, 0, 0, 0)
    radius = 0.25 * upem
    right = max(xmax, 0.95 * advance, 0.55 * upem) if advance > 0 else (xmax if has_outline else 0.95 * upem)
    top = max(ymax, 0.85 * upem)
    return {"cx": right - radius, "cy": top - radius, "radius": radius,
            "clearance": 0.12 * radius, "stroke": 0.16 * radius}


def make_badge_path(spec):
    cx, cy, r = spec["cx"], spec["cy"], spec["radius"]
    ring = pathops.op(circle_path(cx, cy, r),
                      circle_path(cx, cy, r - spec["stroke"]),
                      pathops.PathOp.DIFFERENCE, **OPS)
    stem = pathops.Path()
    pen = stem.getPen()
    pen.moveTo((cx - 0.15 * r, cy + 0.58 * r))
    pen.lineTo((cx + 0.15 * r, cy + 0.58 * r))
    pen.lineTo((cx + 0.11 * r, cy - 0.12 * r))
    pen.lineTo((cx - 0.11 * r, cy - 0.12 * r))
    pen.closePath()
    ring.addPath(stem)
    ring.addPath(circle_path(cx, cy - 0.50 * r, 0.15 * r))
    return pathops.simplify(ring, **OPS)


def marked_path(glyph, upem, advance):
    original = glyph_path(glyph)
    spec = badge_geometry(original, upem, advance)
    # Boolean difference creates closed contours following the circular cut.
    # Painting white over the original would not work on arbitrary backgrounds.
    # Sub-unit cubic coordinates can trigger unstable intersections in Skia
    # (for example Noto U+33A5). Quantize only the cutting operand, not the ink
    # or the visible badge. The resulting boundary differs by less than a unit.
    cutout = pathops.Path()
    circle_path(spec["cx"], spec["cy"], spec["radius"] + spec["clearance"]).draw(
        RoundingPen(cutout.getPen()))
    clipped = pathops.op(original, cutout, pathops.PathOp.DIFFERENCE, **OPS)
    clipped.addPath(make_badge_path(spec))
    return pathops.simplify(clipped, **OPS)


def make_marked_glyph(glyph, upem, advance):
    path = marked_path(glyph, upem, advance)
    pen = TTGlyphPen(None)
    path.draw(Cu2QuPen(pen, max_err=1.0, reverse_direction=False))
    return pen.glyph()
