"""Generate the teal/cyan hexagonal agent badge logo."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 480" width="480" height="480">
  <defs>
    <linearGradient id="hex" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#00d4aa"/>
      <stop offset="100%" stop-color="#00a8e8"/>
    </linearGradient>
    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
      <feGaussianBlur stdDeviation="10" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>

  <!-- Hexagon badge -->
  <path d="M240 40 L400 130 L400 310 L240 400 L80 310 L80 130 Z"
        fill="url(#hex)" stroke="#00d4aa" stroke-width="4" filter="url(#glow)"/>

  <!-- Inner face plate -->
  <path d="M240 110 L325 165 L325 275 L240 330 L155 275 L155 165 Z"
        fill="#050505" stroke="#00d4aa" stroke-width="2" opacity="0.9"/>

  <!-- Antenna -->
  <line x1="240" y1="110" x2="240" y2="75" stroke="#00d4aa" stroke-width="4" stroke-linecap="round"/>
  <circle cx="240" cy="68" r="7" fill="#00d4aa"/>

  <!-- Eyes -->
  <circle cx="190" cy="205" r="18" fill="#00d4aa"/>
  <circle cx="290" cy="205" r="18" fill="#00d4aa"/>

  <!-- Mouth / processor slot -->
  <rect x="190" y="260" width="100" height="14" rx="7" fill="#00d4aa"/>

  <!-- Circuit accents -->
  <polyline points="155 165 130 150 130 120" fill="none" stroke="#00d4aa" stroke-width="3" stroke-linecap="round"/>
  <circle cx="130" cy="115" r="5" fill="#00d4aa"/>
  <polyline points="325 165 350 150 350 120" fill="none" stroke="#00d4aa" stroke-width="3" stroke-linecap="round"/>
  <circle cx="350" cy="115" r="5" fill="#00d4aa"/>
</svg>
"""


def generate(dest: Path, *, width: int = 480) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    svg_path = dest.with_suffix(".svg")
    svg_path.write_text(SVG)
    if sys.platform == "darwin":
        subprocess.run(
            ["sips", "-s", "format", "png", "-Z", str(width), str(svg_path), "--out", str(dest)],
            check=True,
            capture_output=True,
        )
    else:
        # Best-effort fallback; maintainer assets are generated on macOS.
        if (inkscape := shutil.which("inkscape")) is not None:
            subprocess.run(
                [
                    inkscape,
                    str(svg_path),
                    "--export-filename",
                    str(dest),
                    "--export-width",
                    str(width),
                ],
                check=True,
                capture_output=True,
            )
        elif (rsvg := shutil.which("rsvg-convert")) is not None:
            subprocess.run(
                [rsvg, "-w", str(width), "-o", str(dest), str(svg_path)],
                check=True,
                capture_output=True,
            )
        else:
            raise RuntimeError("No SVG rasterizer found for logo generation")
    print(f"Wrote {dest}")


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("assets/logo.png")
    generate(dest)
