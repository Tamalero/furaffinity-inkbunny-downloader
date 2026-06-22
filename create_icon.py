#!/usr/bin/env python3
"""
Generate the 256x256 PNG icon for FA & Inkbunny Downloader.
Run once before building the AppImage (build_appimage.sh calls this automatically).
Requires: pip install pillow
"""
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Pillow not installed. Run: pip install pillow")

OUT  = Path(__file__).parent / "faib-downloader.png"
SIZE = 256
CX   = SIZE // 2

img  = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d    = ImageDraw.Draw(img)

# Background circle — deep indigo
d.ellipse([4, 4, SIZE - 4, SIZE - 4], fill=(24, 18, 48))
# Outer ring
d.ellipse([4, 4, SIZE - 4, SIZE - 4], outline=(100, 70, 200), width=6)

# Download arrow (shaft + head)
d.rectangle([CX - 14, 50, CX + 14, 144], fill=(150, 110, 255))
d.polygon([(CX - 46, 144), (CX + 46, 144), (CX, 196)], fill=(150, 110, 255))

# Completed-download bar at bottom
d.rectangle([48, 208, SIZE - 48, 226], fill=(70, 210, 170))

# Site labels
try:
    font = ImageFont.load_default(size=30)
except TypeError:
    font = ImageFont.load_default()

d.text((22, 22),         "FA", fill=(255, 175, 45),  font=font)
d.text((SIZE - 64, SIZE - 58), "IB", fill=(70, 210, 170), font=font)

img.save(str(OUT), "PNG")
print(f"Saved: {OUT}")
