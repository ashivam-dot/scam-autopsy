"""Generate original Scam Autopsy channel art from simple geometry and text.

Run: python brand/generate.py
Requires Pillow. No downloaded images, icons, or typefaces are embedded.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
NAVY = (11, 22, 41)
MINT = (107, 235, 196)
CORAL = (255, 105, 99)
WHITE = (248, 250, 250)
MUTED = (166, 189, 199)
GRID = (23, 46, 62)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def circle(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, fill: tuple[int, int, int]) -> None:
    draw.ellipse((round(cx - radius), round(cy - radius), round(cx + radius), round(cy + radius)), fill=fill)


def mark(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int) -> None:
    """An open search lens around an alert, with a coral handle."""
    sw = max(6, round(radius * 0.185))
    handle = max(10, round(radius * 0.31))
    x0, y0 = cx + radius * 0.72, cy + radius * 0.72
    x1, y1 = cx + radius * 1.75, cy + radius * 1.75
    draw.line((round(x0), round(y0), round(x1), round(y1)), fill=CORAL, width=handle)
    circle(draw, x0, y0, handle / 2, CORAL)
    circle(draw, x1, y1, handle / 2, CORAL)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=MINT, width=sw)
    # A short white lens glint and the exclamation are custom geometry.
    draw.arc((cx - radius * 0.75, cy - radius * 0.75, cx + radius * 0.75, cy + radius * 0.75), 210, 242, fill=WHITE, width=max(4, round(radius * 0.065)))
    stem = max(8, round(radius * 0.23))
    draw.rounded_rectangle(
        (cx - stem // 2, round(cy - radius * 0.58), cx + stem // 2, round(cy + radius * 0.23)),
        radius=stem // 2, fill=CORAL,
    )
    circle(draw, cx, cy + radius * 0.58, radius * 0.13, CORAL)


def save_small_png(image: Image.Image, path: Path) -> None:
    image.quantize(colors=64, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(path, optimize=True)


def icon() -> None:
    image = Image.new("RGB", (800, 800), NAVY)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((29, 29, 771, 771), radius=102, outline=(51, 83, 94), width=6)
    mark(draw, 330, 310, 175)
    save_small_png(image, ROOT / "scam-autopsy-icon.png")
    # The vector master uses the same coordinates as the Pillow drawing.
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800" viewBox="0 0 800 800" role="img" aria-label="Scam Autopsy alert magnifier">
<rect width="800" height="800" fill="#0b1629"/>
<rect x="29" y="29" width="742" height="742" rx="102" fill="none" stroke="#33535e" stroke-width="6"/>
<path d="M456 436 L636 616" fill="none" stroke="#ff6963" stroke-width="54" stroke-linecap="round"/>
<circle cx="330" cy="310" r="175" fill="none" stroke="#6bebc4" stroke-width="32"/>
<path d="M238 400 A128 128 0 0 1 218 366" fill="none" stroke="#f8fafa" stroke-width="11" stroke-linecap="round"/>
<rect x="310" y="208" width="40" height="142" rx="20" fill="#ff6963"/>
<circle cx="330" cy="411" r="23" fill="#ff6963"/>
</svg>'''
    (ROOT / "scam-autopsy-icon.svg").write_text(svg + "\n", encoding="utf-8")


def banner() -> None:
    width, height = 2560, 1440
    image = Image.new("RGB", (width, height), NAVY)
    draw = ImageDraw.Draw(image)
    # Subtle grid and oversized forensic rings sit behind the center safe area.
    for x in range(0, width, 96):
        draw.line((x, 0, x, height), fill=GRID, width=1)
    for y in range(0, height, 96):
        draw.line((0, y, width, y), fill=GRID, width=1)
    draw.ellipse((-370, 140, 650, 1160), outline=(30, 75, 79), width=4)
    draw.ellipse((1960, 340, 2920, 1300), outline=(54, 48, 64), width=4)
    draw.line((0, 720, 2560, 720), fill=(28, 58, 72), width=2)
    mark(draw, 720, 682, 119)
    title_font = font(108, bold=True)
    tagline_font = font(49, bold=True)
    detail_font = font(34)
    title = "SCAM AUTOPSY"
    tagline = "Spot the scam. Keep your money."
    detail = "Messages  •  Money trails  •  Red flags"
    text_x = 955
    title_y, tagline_y, detail_y = 545, 694, 790
    safe = (507, 509, 2053, 932)
    for label, y, selected_font in ((title, title_y, title_font), (tagline, tagline_y, tagline_font), (detail, detail_y, detail_font)):
        bounds = draw.textbbox((text_x, y), label, font=selected_font)
        if bounds[0] < safe[0] or bounds[1] < safe[1] or bounds[2] > safe[2] or bounds[3] > safe[3]:
            raise ValueError(f"Banner text outside YouTube safe area: {label}: {bounds}")
    draw.text((text_x, title_y), title, font=title_font, fill=WHITE)
    draw.rounded_rectangle((text_x, 670, 1515, 679), radius=4, fill=CORAL)
    draw.text((text_x, tagline_y), tagline, font=tagline_font, fill=MINT)
    draw.text((text_x, detail_y), detail, font=detail_font, fill=MUTED)
    save_small_png(image, ROOT / "scam-autopsy-banner.png")


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    icon()
    banner()
    for path in sorted(ROOT.glob("scam-autopsy-*")):
        print(f"{path.name}: {path.stat().st_size} bytes")
