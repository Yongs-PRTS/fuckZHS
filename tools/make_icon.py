"""Generate the application icon without external image assets."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    output = Path(sys.argv[1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((17, 17, 239, 239), radius=68, fill="#d8f36a", outline="#171b18", width=12)
    points = [(83, 78), (177, 78), (98, 179), (179, 179)]
    draw.line(points, fill="#171b18", width=28, joint="curve")
    draw.ellipse((171, 171, 187, 187), fill="#171b18")
    image.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
