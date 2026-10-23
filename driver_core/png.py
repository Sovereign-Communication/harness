"""Strict PNG raster validation for locally prepared screen captures."""
import struct
import zlib


SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SCANLINE_BYTES = 65536
MAX_DECODED_RASTER_BYTES = 64 * 1024 * 1024


def dimensions(data):
    """Return PNG IHDR dimensions, or None when the header is incomplete."""
    if (not isinstance(data, bytes) or len(data) < 24
            or data[:8] != SIGNATURE
            or data[8:12] != b"\x00\x00\x00\x0d"
            or data[12:16] != b"IHDR"):
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return (width, height) if width and height else None


def decodable(data):
    """Validate chunks, checksums, DEFLATE completion and every scanline.

    Inflation and each scanline buffer are capped at 64 KiB, and total raster
    output is capped at 64 MiB before inflation. A forged IHDR cannot force an
    oversized row or unbounded decode work before the compositor sees it.
    Animated PNGs are refused because a screenshot asset must be a still.
    """
    if not isinstance(data, bytes):
        return False
    try:
        _validate(data)
    except (ValueError, struct.error, zlib.error):
        return False
    return True


def _validate(data):
    if data[:8] != SIGNATURE:
        raise ValueError("signature")
    offset = 8
    header = None
    palette = None
    compressed = []
    ended = False
    while offset < len(data):
        length, = struct.unpack_from(">I", data, offset)
        kind = data[offset + 4:offset + 8]
        start, end = offset + 8, offset + 8 + length
        payload = data[start:end]
        crc, = struct.unpack_from(">I", data, end)
        if zlib.crc32(kind + payload) != crc:
            raise ValueError("CRC")
        offset = end + 4
        if header is None:
            if kind != b"IHDR" or length != 13:
                raise ValueError("IHDR")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            if (compressed or palette is not None or not 0 < length <= 768
                    or length % 3):
                raise ValueError("palette")
            palette = length // 3
        elif kind == b"IDAT":
            if ended:
                raise ValueError("non-contiguous IDAT")
            compressed.append(payload)
        elif kind == b"IEND":
            if length or offset != len(data):
                raise ValueError("IEND")
            break
        elif (kind in (b"acTL", b"fcTL", b"fdAT")
              or not kind[0] & 32):
            raise ValueError("unsupported critical/animated chunk")
        elif compressed:
            ended = True
    else:
        raise ValueError("missing IEND")

    width, height, depth, color, compression, filtering, interlace = header
    depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
              4: (8, 16), 6: (8, 16)}
    if (not width or not height or depth not in depths.get(color, ())
            or compression or filtering or interlace not in (0, 1)
            or not compressed):
        raise ValueError("unsupported raster")
    if color == 3 and (palette is None or palette > 2 ** depth):
        raise ValueError("missing/oversized palette")

    bits = depth * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = ((0, 0, 1, 1),) if not interlace else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    pass_sizes = []
    decoded_bytes = 0
    for x, y, dx, dy in passes:
        pass_width = max(0, (width - x + dx - 1) // dx)
        pass_height = max(0, (height - y + dy - 1) // dy)
        if pass_width and pass_height:
            row_bytes = (pass_width * bits + 7) // 8
            if row_bytes + 1 > MAX_SCANLINE_BYTES:
                raise ValueError("scanline exceeds buffer bound")
            decoded_bytes += (row_bytes + 1) * pass_height
            if decoded_bytes > MAX_DECODED_RASTER_BYTES:
                raise ValueError("raster exceeds decoded-byte bound")
            pass_sizes.append((pass_width, pass_height))
    rows = ((width_for_pass, row == 0)
            for width_for_pass, height_for_pass in pass_sizes
            for row in range(height_for_pass))
    current = next(rows, None)
    previous = b""
    buffer = bytearray()
    decoder = zlib.decompressobj()
    pending = b"".join(compressed)
    while True:
        max_output = MAX_SCANLINE_BYTES - len(buffer)
        block = decoder.decompress(pending, max_output)
        pending = decoder.unconsumed_tail
        buffer.extend(block)
        while current is not None:
            width_for_row, first = current
            size = (width_for_row * bits + 7) // 8
            if len(buffer) < size + 1:
                break
            method = buffer[0]
            if method > 4:
                raise ValueError("scanline filter")
            if color == 3:
                previous = _palette_row(
                    buffer[1:size + 1], method,
                    b"" if first else previous)
                mask = (1 << depth) - 1
                if any((previous[i * depth // 8] >>
                        (8 - depth - i * depth % 8)) & mask >= palette
                       for i in range(width_for_row)):
                    raise ValueError("palette index")
            del buffer[:size + 1]
            current = next(rows, None)
        if current is None and buffer:
            raise ValueError("excess raster bytes")
        if not pending and len(block) < max_output:
            break
    if (not decoder.eof or decoder.unused_data or current is not None
            or buffer):
        raise ValueError("incomplete raster")


def _palette_row(row, method, previous):
    """Undo PNG filters for indexed pixels so invalid indices cannot hide."""
    result = bytearray(row)
    for i, value in enumerate(row):
        left = result[i - 1] if i else 0
        up = previous[i] if previous else 0
        corner = previous[i - 1] if previous and i else 0
        p = left + up - corner
        distances = (abs(p - left), abs(p - up), abs(p - corner))
        paeth = (left, up, corner)[distances.index(min(distances))]
        result[i] = (value + (0, left, up, (left + up) // 2, paeth)[method]) % 256
    return result
