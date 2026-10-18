"""Regenerate the demo video in docs/demo/ (shown at the top of the README).

    python scripts/demo/build.py

Needs the e2e extra (``pip install -e '.[e2e]'`` + ``playwright install chromium``),
``numpy`` and ``ffmpeg``. Set ``OT_CHROMIUM`` to use a specific Chromium binary.

Records the real app live (``record.py``), scores it (``soundtrack.py``) and writes:

- ``demo.mp4``: 1920x1080, 30 fps, with sound; its first frame is the poster
- ``demo.gif``: a silent 960 px loop that plays inline on GitHub
- ``demo.jpg``: the poster frame
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import record  # noqa: E402 - needs the path above
import soundtrack  # noqa: E402

ROOT = HERE.parents[1]
WORK = ROOT / "build" / "demo"
OUT = ROOT / "docs" / "demo"
FPS = record.FPS


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)  # noqa: S603, S607


def main() -> None:
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is needed to encode the video")
    events = record.record(WORK, os.environ.get("OT_CHROMIUM") or None)
    frames = sorted((WORK / "frames").glob("*.jpg"))
    duration = len(frames) / FPS
    soundtrack.render(events, duration, WORK / "audio.wav")

    # Poster: both devices just showed "Delivered". It is also frame 0, so every
    # player's thumbnail shows it (replacing a frame keeps the audio in sync).
    poster = frames[min(len(frames) - 1, round((events["delivered"] + 1.2) * FPS))]
    final = WORK / "final"
    shutil.rmtree(final, ignore_errors=True)
    shutil.copytree(WORK / "frames", final)
    shutil.copy2(poster, final / "0000.jpg")

    OUT.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", str(poster), "-q:v", "2", str(OUT / "demo.jpg"))
    ffmpeg(
        "-framerate", str(FPS), "-i", str(final / "%04d.jpg"), "-i", str(WORK / "audio.wav"),
        "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=lanczos,format=yuv420p",
        "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "18",
        "-tune", "stillimage", "-g", str(FPS * 2),
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", "-shortest", str(OUT / "demo.mp4"),
    )  # fmt: skip
    ffmpeg(  # from the raw frames: a poster frame 0 would flash on every loop
        "-framerate", str(FPS), "-i", str(WORK / "frames" / "%04d.jpg"),
        "-vf", "fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
        "-loop", "0", str(OUT / "demo.gif"),
    )  # fmt: skip
    (WORK / "events.json").write_text(json.dumps(events, indent=1), encoding="utf-8")
    sizes = ", ".join(f"{p.name} {p.stat().st_size / 1e6:.1f} MB" for p in sorted(OUT.iterdir()))
    print(f"Saved {OUT.relative_to(ROOT)}/ ({duration:.1f} s): {sizes}")


if __name__ == "__main__":
    main()
