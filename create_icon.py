"""Generate the Windows application icon from deterministic vector-like shapes."""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
ASSET_DIR = ROOT / "assets"
PNG_PATH = ASSET_DIR / "deploy-console-parallel.png"
ICO_PATH = ASSET_DIR / "deploy-console-parallel.ico"


def rounded_polygon(draw, points, radius, fill):
    """Draw a compact rounded rectangle from its bounding coordinates."""
    draw.rounded_rectangle(points, radius=radius, fill=fill)


def build_icon():
    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Quiet navy shell, cyan execution lanes, and a green release arrow keep
    # the mark recognizable at taskbar size without relying on text.
    draw.rounded_rectangle((56, 56, 968, 968), radius=210, fill="#0B1220")
    draw.rounded_rectangle(
        (84, 84, 940, 940), radius=180, outline="#29415F", width=24
    )

    for x in (242, 418, 594):
        rounded_polygon(draw, (x, 690, x + 132, 822), 28, "#132A42")
        rounded_polygon(draw, (x + 20, 710, x + 112, 802), 20, "#38BDF8")

    draw.line((308, 662, 308, 560, 512, 560), fill="#38BDF8", width=42)
    draw.line((484, 662, 484, 478, 610, 478), fill="#38BDF8", width=42)
    draw.line((660, 662, 660, 598), fill="#38BDF8", width=42)

    draw.rounded_rectangle((226, 200, 674, 430), radius=76, fill="#F8FAFC")
    draw.rounded_rectangle((260, 234, 640, 396), radius=50, fill="#19314D")
    draw.ellipse((292, 285, 340, 333), fill="#38BDF8")
    draw.ellipse((366, 285, 414, 333), fill="#22C55E")

    # Release arrow crosses the service card and lands on the execution lanes.
    arrow = [
        (590, 330), (742, 330), (742, 252), (910, 420),
        (742, 588), (742, 510), (590, 510),
    ]
    draw.polygon(arrow, fill="#22C55E")
    draw.line((606, 420, 774, 420), fill="#C7F9D8", width=24)

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    image.save(PNG_PATH, optimize=True)
    image.save(
        ICO_PATH,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
               (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    build_icon()
