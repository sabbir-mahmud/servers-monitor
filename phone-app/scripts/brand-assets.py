"""Draw the app's icons and splash from the NexGuard logo (main/static/img/icon.svg): a
blue gradient tile with a white shield and a pulse line. Run from the repo's venv after
changing the logo:

    python phone-app/scripts/brand-assets.py

Writes phone-app/assets/: icon.png (iOS, full-bleed; iOS rounds it), the Android adaptive
icon layers, the splash image, the in-app logo and the web favicon.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SS = 4  # supersampling for smooth edges

# The SVG's radial gradient: centre (20%, 0%), radius 120% of the tile.
STOPS = [(0.0, (0x7B, 0x97, 0xFF)), (0.45, (0x4A, 0x6C, 0xF0)), (1.0, (0x2C, 0x45, 0xB8))]
BRAND = "#4a6cf0"


def gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    cx, cy, r = 0.2 * size, 0.0, 1.2 * size
    for y in range(size):
        for x in range(size):
            t = min(1.0, ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / r)
            for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
                if t <= t1:
                    k = (t - t0) / (t1 - t0)
                    px[x, y] = tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))
                    break
    return img


def cubic(p0, p1, p2, p3, steps=40):
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        out.append(tuple(u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d
                         for a, b, c, d in zip(p0, p1, p2, p3)))  # fmt: skip
    return out


# The logo's paths, in its 24-unit icon space.
SHIELD = (
    [(12, 2.75), (4.75, 5.6), (4.75, 11.25)]
    + cubic((4.75, 11.25), (4.75, 15.7), (7.77, 19.67), (12, 21.15))
    + cubic((12, 21.15), (16.23, 19.67), (19.25, 15.7), (19.25, 11.25))
    + [(19.25, 5.6)]
)
PULSE = [(7.6, 12.4), (9.7, 12.4), (11.0, 9.5), (12.9, 14.7), (14.2, 12.4), (16.4, 12.4)]


def mark(size: int, unit: float, origin: tuple[float, float], fill_alpha: int = 36) -> Image.Image:
    """The white shield and pulse on a transparent layer: `unit` pixels per icon unit,
    the icon space's (0, 0) at `origin`."""
    big = size * SS
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    u, (ox, oy) = unit * SS, (origin[0] * SS, origin[1] * SS)

    def pts(path):
        return [(ox + x * u, oy + y * u) for x, y in path]

    def stroke(path, width, closed=False):
        p = pts(path)
        if closed:
            p = p + p[:1]
        w = width * u
        draw.line(p, fill=(255, 255, 255, 255), width=round(w), joint="curve")
        for x, y in p:  # round caps and joins
            draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=(255, 255, 255, 255))

    draw.polygon(pts(SHIELD), fill=(255, 255, 255, fill_alpha))
    stroke(SHIELD, 1.5, closed=True)
    stroke(PULSE, 1.6)
    return layer.resize((size, size), Image.LANCZOS)


def rounded_mask(size: int, radius_ratio: float = 112 / 512) -> Image.Image:
    big = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, size * SS - 1, size * SS - 1),
                                          radius=radius_ratio * size * SS, fill=255)  # fmt: skip
    return big.resize((size, size), Image.LANCZOS)


def tile(size: int, rounded: bool) -> Image.Image:
    """The whole logo: as in the SVG, the icon space is 75% of the tile, centred."""
    img = gradient(size).convert("RGBA")
    unit = size * 0.75 / 24
    img.alpha_composite(mark(size, unit, (size * 0.125, size * 0.125)))
    if rounded:
        img.putalpha(rounded_mask(size))
    return img


def centred_mark(size: int, share: float) -> Image.Image:
    """Just the mark, its 24-unit icon space taking `share` of the canvas."""
    unit = size * share / 24
    offset = (size - 24 * unit) / 2
    return mark(size, unit, (offset, offset))


def main() -> None:
    tile(1024, rounded=False).convert("RGB").save(ASSETS / "icon.png")
    # Android adaptive icon (108 dp, the middle 66 dp always shows): gradient behind, the
    # mark inside the safe zone in front, and a one-colour version for themed icons.
    gradient(512).save(ASSETS / "android-icon-background.png")
    centred_mark(512, 0.62).save(ASSETS / "android-icon-foreground.png")
    centred_mark(432, 0.62).save(ASSETS / "android-icon-monochrome.png")
    tile(1024, rounded=True).save(ASSETS / "splash-icon.png")
    tile(256, rounded=True).save(ASSETS / "logo.png")
    tile(48, rounded=True).save(ASSETS / "favicon.png")
    print(f"wrote {ASSETS}")


if __name__ == "__main__":
    main()
