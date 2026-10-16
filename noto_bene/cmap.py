"""Keep the Windows BMP character map within format 4's 16-bit size limit."""

import struct


def prepare_windows_bmp_cmap(font):
    """Use fontTools' encoding when it fits, or share repeated glyph arrays.

    Dense fallback coverage can overflow fontTools' format-4 encoding even
    when most characters map to the same glyph. Splitting consecutive ranges
    into short segments lets those segments share identical glyph-ID arrays.
    """
    for table in font["cmap"].tables:
        if (table.platformID, table.platEncID, table.format) != (3, 1, 4):
            continue
        mapping = table.cmap  # Also materialize a lazily loaded subtable.
        table.data = None  # Discard an earlier cache if the mapping changed.
        try:
            table.compile(font)
            continue
        except (struct.error, OverflowError):
            pass

        segments = []
        glyph_ids = []
        arrays = {}

        def add_segment(codes, indices):
            start, end = codes[0], codes[-1]
            if all(gid == indices[0] + i for i, gid in enumerate(indices)):
                segments.append((start, end, (indices[0] - start) & 0xFFFF, None))
                return
            key = tuple(indices)
            if key not in arrays:
                arrays[key] = len(glyph_ids)
                glyph_ids.extend(indices)
            segments.append((start, end, 0, arrays[key]))

        codes, indices = [], []
        for code, name in sorted(mapping.items()):
            if not 0 <= code < 0xFFFF:
                raise ValueError("Windows BMP cmap format 4 requires codepoints below U+FFFF")
            gid = font.getGlyphID(name)
            if not 0 <= gid <= 0xFFFF:
                raise ValueError("Windows BMP cmap glyph ID exceeds the uint16 range")
            if codes and (code != codes[-1] + 1 or len(codes) == 256):
                add_segment(codes, indices)
                codes, indices = [], []
            codes.append(code)
            indices.append(gid)
        if codes:
            add_segment(codes, indices)
        segments.append((0xFFFF, 0xFFFF, 1, None))

        count = len(segments)
        length = 16 + 8 * count + 2 * len(glyph_ids)
        if length > 0xFFFF:
            raise ValueError("Windows BMP cmap cannot fit in format 4 without losing coverage")
        offsets = [0 if index is None else 2 * (count + index - i)
                   for i, (_, _, _, index) in enumerate(segments)]
        if not 0 <= table.language <= 0xFFFF or any(offset > 0xFFFF for offset in offsets):
            raise ValueError("Windows BMP cmap field exceeds the uint16 range")
        selector = count.bit_length() - 1
        search_range = 2 * (1 << selector)
        words = ([end for _, end, _, _ in segments] + [0]
                 + [start for start, _, _, _ in segments]
                 + [delta for _, _, delta, _ in segments]
                 + offsets + glyph_ids)
        raw = (struct.pack(">7H", 4, length, table.language, 2 * count,
                           search_range, selector, 2 * count - search_range)
               + struct.pack(f">{len(words)}H", *words))
        table.length = length
        table.data = raw[6:]
