"""Open one camera directly for diagnosis, without starting the FastAPI server."""

from __future__ import annotations

import argparse
import sys

import cv2
import numpy as np


def _parse_source(value: str) -> int | str:
    return int(value) if value.lstrip("-").isdigit() else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="0", help="DirectShow index or stream URL")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--no-display", action="store_true")
    args = parser.parse_args()

    source = _parse_source(args.source)
    camera = (
        cv2.VideoCapture(source, cv2.CAP_DSHOW)
        if isinstance(source, int)
        else cv2.VideoCapture(source)
    )
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    camera.set(cv2.CAP_PROP_FPS, args.fps)
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not camera.isOpened():
        print(f"ERROR: Could not open camera source {args.source!r}")
        return 1

    print(f"Opened camera source {args.source!r}. Press Q or Escape to close.")
    black_frames = 0
    frames = 0
    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                print("ERROR: Camera frame lost")
                return 1
            frames += 1
            mean = float(frame.mean())
            is_black = mean < 1.0 and float(frame.std()) < 1.0
            black_frames = black_frames + 1 if is_black else 0
            if black_frames == 8:
                print("ERROR: Camera is returning all-black frames; check DroidCam output.")

            if not args.no_display:
                status = "BLACK FRAME" if is_black else f"brightness {mean:.1f}"
                cv2.putText(
                    frame,
                    f"Source {args.source}: {status}",
                    (16, 32),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.75,
                    (0, 0, 255) if is_black else (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
                cv2.imshow("Welcome Robot - Raw Camera Preview", frame)
                if cv2.waitKey(1) & 0xFF in {ord("q"), 27}:
                    return 0

            if args.max_frames and frames >= args.max_frames:
                return 0
    finally:
        camera.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    sys.exit(main())
