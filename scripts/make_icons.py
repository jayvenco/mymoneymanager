"""Genereert de PNG-iconen (PWA + Unraid). Vereist Pillow (alleen dev)."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent


def render(size: int) -> Image.Image:
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(s):  # verticale verloop teal-500 -> teal-700
        t = y / s
        color = (
            int(20 + (15 - 20) * t),
            int(184 + (118 - 184) * t),
            int(166 + (110 - 166) * t),
            255,
        )
        d.line([(0, y), (s, y)], fill=color)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.23), fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    u = s / 128
    pts = [(34 * u, 92 * u), (34 * u, 44 * u), (64 * u, 74 * u), (94 * u, 44 * u), (94 * u, 92 * u)]
    d.line(pts, fill="white", width=int(10 * u), joint="curve")
    for x, y in (pts[0], pts[-1], pts[1], pts[3]):
        r = 5 * u
        d.ellipse([x - r, y - r, x + r, y + r], fill="white")
    return img.resize((size, size), Image.LANCZOS)


for size in (192, 512):
    render(size).save(ROOT / f"app/static/icons/icon-{size}.png")
render(256).save(ROOT / "unraid/icon.png")
print("iconen geschreven")
