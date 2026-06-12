"""Generates desktop_labeller.ico, a small dependency-free app/tray icon.

The icon depicts a dark rounded tile holding a 2x2 grid of amber workspace
squares (one highlighted "active"), matching the overlay's color theme.

Rendering uses supersampling so the rounded corners stay smooth at every size.
Run with the project Python:  python make_icon.py
"""
import struct

OUTPUT = "desktop_labeller.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 4  # supersampling factor for anti-aliasing

# Theme colors (R, G, B)
TILE = (34, 17, 0)        # dark amber-brown background tile (#221100)
CELL_DIM = (133, 102, 31)   # inactive workspace square
CELL_ACTIVE = (255, 179, 0)  # active workspace square (#FFB300)


def _inside_rounded(px, py, x0, y0, x1, y1, r):
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    dx = px - cx
    dy = py - cy
    return dx * dx + dy * dy <= r * r


def _render(size):
    """Returns a list of (b, g, r, a) tuples, top-to-bottom, for the size."""
    m = size * SS

    pad = size * 0.12 * SS
    gap = size * 0.10 * SS
    tile_r = size * 0.20 * SS
    inner = m - 2 * pad
    cell = (inner - gap) / 2.0
    cell_r = cell * 0.22

    # Top-left corners of the 2x2 cells
    cells = []
    for row in range(2):
        for col in range(2):
            cx0 = pad + col * (cell + gap)
            cy0 = pad + row * (cell + gap)
            active = (row, col) == (0, 0)
            cells.append((cx0, cy0, cx0 + cell, cy0 + cell, active))

    out = []
    block = SS * SS
    for ty in range(size):
        for tx in range(size):
            inside = 0
            sr = sg = sb = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = tx * SS + sx + 0.5
                    py = ty * SS + sy + 0.5
                    color = None
                    # Cells draw on top of the tile
                    for (x0, y0, x1, y1, active) in cells:
                        if _inside_rounded(px, py, x0, y0, x1, y1, cell_r):
                            color = CELL_ACTIVE if active else CELL_DIM
                            break
                    if color is None and _inside_rounded(
                        px, py, 0, 0, m - 1, m - 1, tile_r
                    ):
                        color = TILE
                    if color is not None:
                        inside += 1
                        sr += color[0]
                        sg += color[1]
                        sb += color[2]
            a = int(round(inside / block * 255))
            if inside > 0:
                r = sr // inside
                g = sg // inside
                b = sb // inside
            else:
                r = g = b = 0
            out.append((b, g, r, a))
    return out


def _bmp_image(size, pixels):
    """Builds an ICO-embedded BMP (BITMAPINFOHEADER + BGRA + AND mask)."""
    # BITMAPINFOHEADER: height is doubled to account for the AND mask.
    header = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        size,          # biWidth
        size * 2,      # biHeight (XOR + AND)
        1,             # biPlanes
        32,            # biBitCount
        0,             # biCompression (BI_RGB)
        0,             # biSizeImage
        0, 0,          # resolution
        0, 0,          # colors
    )

    # XOR (color) data is stored bottom-up.
    xor = bytearray()
    for y in range(size - 1, -1, -1):
        row = pixels[y * size:(y + 1) * size]
        for (b, g, r, a) in row:
            xor += bytes((b, g, r, a))

    # AND mask: 1 bpp, rows padded to 32-bit boundary. All zero = use alpha.
    mask_row_bytes = ((size + 31) // 32) * 4
    and_mask = bytes(mask_row_bytes * size)

    return header + bytes(xor) + and_mask


def build():
    images = []
    for size in SIZES:
        pixels = _render(size)
        images.append((size, _bmp_image(size, pixels)))

    count = len(images)
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, count)  # ICONDIR

    offset = 6 + 16 * count
    for size, data in images:
        w = size if size < 256 else 0
        h = size if size < 256 else 0
        out += struct.pack(
            "<BBBBHHII",
            w, h,
            0,            # color count
            0,            # reserved
            1,            # planes
            32,           # bit count
            len(data),
            offset,
        )
        offset += len(data)

    for _, data in images:
        out += data

    with open(OUTPUT, "wb") as fh:
        fh.write(out)
    print(f"Wrote {OUTPUT} ({len(out)} bytes, sizes: {SIZES})")


if __name__ == "__main__":
    build()
