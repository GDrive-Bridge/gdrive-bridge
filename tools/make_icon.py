"""Draw icon.png (512x512) and fanart.jpg (1920x1080) for GDrive Bridge.

A play-button triangle in Drive's green/yellow/blue, with a Kodi-blue core,
on a Kodi-style dark navy tile. Run with a Python that has Pillow.
"""
import math
import sys
from PIL import Image, ImageDraw, ImageFilter

SS = 4                      # supersampling factor
GREEN, YELLOW, BLUE = (52, 168, 83), (251, 188, 4), (66, 133, 244)
KODI_BLUE = (23, 178, 231)
NAVY_TOP, NAVY_BOT = (36, 50, 64), (16, 23, 30)


def lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def offset_triangle(pts, d):
    """Move each edge of a CCW/CW triangle inwards by d and return the new vertices."""
    cx = sum(p[0] for p in pts) / 3
    cy = sum(p[1] for p in pts) / 3
    lines = []
    for i in range(3):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % 3]
        nx, ny = y2 - y1, -(x2 - x1)
        n = math.hypot(nx, ny)
        nx, ny = nx / n, ny / n
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if (cx - mx) * nx + (cy - my) * ny < 0:
            nx, ny = -nx, -ny
        lines.append(((x1 + nx * d, y1 + ny * d), (x2 + nx * d, y2 + ny * d)))
    out = []
    for i in range(3):
        (p1, p2), (p3, p4) = lines[i - 1], lines[i]
        x1, y1 = p1; x2, y2 = p2; x3, y3 = p3; x4, y4 = p4
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
        out.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
    return out


def draw_mark(size, radius=0.0):
    """The tile and mark at `size` px (drawn at SS x and downsampled).

    Kodi skins frame and round icons themselves, so the icon is a full square
    (radius 0); a transparent rounded corner shows as dark wedges in the frame.
    """
    S = size * SS
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    # tile with vertical gradient
    grad = Image.new('RGBA', (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        gd.line([(0, y), (S, y)], fill=lerp(NAVY_TOP, NAVY_BOT, y / S) + (255,))
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * radius), fill=255)
    img.paste(grad, (0, 0), mask)
    # soft Kodi-blue glow behind the mark
    glow = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    r = S * 0.30
    ImageDraw.Draw(glow).ellipse([S / 2 - r, S / 2 - r, S / 2 + r, S / 2 + r], fill=KODI_BLUE + (70,))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.08))
    img = Image.alpha_composite(img, Image.composite(glow, Image.new('RGBA', (S, S)), mask))
    d = ImageDraw.Draw(img)
    # play triangle (pointing right), optically centred
    u = S / 512
    A, B, C = (172 * u, 104 * u), (172 * u, 408 * u), (436 * u, 256 * u)
    outer = [A, C, B]                     # top edge, bottom edge, left edge order
    inner = offset_triangle(outer, 44 * u)
    core = offset_triangle(outer, 60 * u)
    colours = [YELLOW, BLUE, GREEN]       # edges A-C (top), C-B (bottom), B-A (left)
    for i in range(3):
        band = [outer[i], outer[(i + 1) % 3], inner[(i + 1) % 3], inner[i]]
        d.polygon(band, fill=colours[i] + (255,))
    # thin separators so the three bands read as separate pieces
    for i in range(3):
        d.line([outer[i], inner[i]], fill=lerp(NAVY_TOP, NAVY_BOT, 0.5) + (255,), width=int(5 * u))
    d.polygon(core, fill=KODI_BLUE + (255,))
    return img.resize((size, size), Image.LANCZOS)


def main(out_dir):
    icon = draw_mark(512)
    icon.save(out_dir + '/icon.png', optimize=True)
    W, H = 1920, 1080
    fan = Image.new('RGB', (W, H))
    fd = ImageDraw.Draw(fan)
    for y in range(H):
        fd.line([(0, y), (W, y)], fill=lerp(NAVY_TOP, NAVY_BOT, y / H))
    mark = draw_mark(560, radius=0.19)
    fan.paste(mark, ((W - 560) // 2, (H - 560) // 2), mark)
    fan.save(out_dir + '/fanart.jpg', quality=90)


if __name__ == '__main__':
    main(sys.argv[1])
