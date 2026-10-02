"""Generate the figures embedded in the corpus PDFs.

Figures are drawn programmatically rather than sourced as stock photography: it keeps the
repository free of third-party licensed binaries, and it means a figure is reproducible from
the script that made it rather than being an opaque asset nobody can regenerate.

Output goes to corpus/figures/, which is gitignored -- these are build artifacts.

IMPORTANT, and the reason every figure in the corpus carries a caption: pypdf extracts TEXT.
An image embedded in a PDF is invisible to the retrieval pipeline. No fact may exist only in a
figure; figures illustrate text that also states the fact. See the corpus README.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "corpus" / "figures"

INK = (31, 41, 55)
MUTED = (107, 114, 128)
ACCENT = (15, 76, 117)
LAKE = (21, 94, 117)
PAPER = (255, 255, 255)
WASH = (241, 245, 249)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Best-effort system font, falling back to PIL's bitmap default."""
    for name in (("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def wordmark(path: Path, title: str, subtitle: str, accent: tuple[int, int, int]) -> None:
    """A letterhead mark: rule, name, and a small device. Deliberately simple."""
    img = Image.new("RGB", (1400, 300), PAPER)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, 18, 300], fill=accent)

    d.text((70, 72), title, font=_font(78, bold=True), fill=INK)
    d.text((74, 168), subtitle.upper(), font=_font(26), fill=MUTED)

    # A small device at the right edge: three stacked waves, which reads as water at any size.
    for i, y in enumerate((112, 150, 188)):
        d.arc([1190, y - 30, 1330, y + 30], start=190, end=350, fill=accent, width=8 - i * 2)

    img.save(path, "PNG")


def meeting_layouts(path: Path) -> None:
    """Four seating layouts for the Harborview meeting room."""
    img = Image.new("RGB", (1500, 430), PAPER)
    d = ImageDraw.Draw(img)
    f_title = _font(30, bold=True)
    f_cap = _font(24)

    def seat(cx: float, cy: float, r: int = 9) -> None:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)

    panels = [
        ("Boardroom", 20),
        ("Theatre", 20),
        ("U-shape", 14),
        ("Classroom", 12),
    ]

    for idx, (name, count) in enumerate(panels):
        ox = 40 + idx * 365
        d.rectangle([ox, 30, ox + 320, 330], outline=MUTED, width=3, fill=WASH)
        d.text((ox, 345), name, font=f_title, fill=INK)
        d.text((ox, 385), f"seats {count}", font=f_cap, fill=MUTED)

        cx, cy = ox + 160, 180
        if name == "Boardroom":
            d.rectangle([cx - 90, cy - 45, cx + 90, cy + 45], fill=(255, 255, 255), outline=INK, width=3)
            for i in range(7):
                x = cx - 78 + i * 26
                seat(x, cy - 68)
                seat(x, cy + 68)
            seat(cx - 115, cy)
            seat(cx + 115, cy)
        elif name == "Theatre":
            for row in range(4):
                for col in range(5):
                    seat(cx - 80 + col * 40, cy - 60 + row * 42)
        elif name == "U-shape":
            d.line([cx - 80, cy - 50, cx - 80, cy + 60], fill=INK, width=8)
            d.line([cx + 80, cy - 50, cx + 80, cy + 60], fill=INK, width=8)
            d.line([cx - 80, cy - 50, cx + 80, cy - 50], fill=INK, width=8)
            for i in range(5):
                seat(cx - 110, cy - 30 + i * 24)
                seat(cx + 110, cy - 30 + i * 24)
            for i in range(4):
                seat(cx - 54 + i * 36, cy - 78)
        else:  # Classroom
            for row in range(3):
                y = cy - 55 + row * 55
                d.rectangle([cx - 85, y - 10, cx + 85, y + 8], fill=(255, 255, 255), outline=INK, width=3)
                for col in range(4):
                    seat(cx - 64 + col * 43, y - 32)

    img.save(path, "PNG")


def locator(path: Path, title: str, here: str, landmarks: list[str], accent: tuple[int, int, int]) -> None:
    """A schematic locator sketch. Not to scale, and labelled as such."""
    img = Image.new("RGB", (1500, 620), PAPER)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, 1500, 86], fill=accent)
    d.text((34, 24), title, font=_font(40, bold=True), fill=PAPER)

    # Water body along the lower edge.
    d.rectangle([0, 430, 1500, 620], fill=(219, 234, 254))
    for y in (470, 520, 570):
        d.arc([120, y - 18, 420, y + 18], start=190, end=350, fill=(147, 197, 253), width=5)
        d.arc([900, y - 18, 1200, y + 18], start=190, end=350, fill=(147, 197, 253), width=5)
    d.text((34, 560), "water", font=_font(26), fill=(59, 130, 246))

    # Two roads.
    d.line([0, 300, 1500, 300], fill=MUTED, width=14)
    d.line([560, 86, 560, 620], fill=MUTED, width=14)

    # The property.
    d.rectangle([600, 330, 860, 420], fill=accent)
    d.text((614, 352), here, font=_font(30, bold=True), fill=PAPER)

    for i, label in enumerate(landmarks[:3]):
        x = 120 + i * 430
        d.rectangle([x, 150, x + 300, 250], outline=INK, width=3, fill=WASH)
        d.text((x + 16, 182), label, font=_font(26), fill=INK)

    d.text((34, 600 - 28), "Schematic. Not to scale.", font=_font(22), fill=MUTED)
    img.save(path, "PNG")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    wordmark(OUT / "logo-harborview.png", "Harborview Grand", "Portland, Maine", ACCENT)
    wordmark(OUT / "logo-lakeside.png", "Lakeside Inn", "Burlington, Vermont", LAKE)
    wordmark(OUT / "logo-brand.png", "HotelApp Hotels", "Harborview Grand · Lakeside Inn", INK)

    meeting_layouts(OUT / "meeting-layouts.png")

    locator(
        OUT / "locator-harborview.png",
        "Harborview Grand — Wharf Street",
        "Harborview\nGrand",
        ["Ferry terminal", "Commercial St", "Fore St Garage"],
        ACCENT,
    )
    locator(
        OUT / "locator-lakeside.png",
        "Lakeside Inn — North Shore Road",
        "Lakeside\nInn",
        ["North Ave", "Island Line Trail", "North Beach"],
        LAKE,
    )

    for p in sorted(OUT.glob("*.png")):
        print(f"  {p.name}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
