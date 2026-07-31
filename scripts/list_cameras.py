"""List Windows camera indices so DroidCam Video can be selected in .env."""

import cv2


def main() -> None:
    found = 0
    print("Scanning DirectShow camera indices 0-9…")
    for index in range(10):
        camera = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        try:
            if not camera.isOpened():
                continue
            ok, frame = camera.read()
            if not ok:
                continue
            height, width = frame.shape[:2]
            print(f"CAMERA_SOURCE={index}  frame={width}x{height}")
            found += 1
        finally:
            camera.release()
    if not found:
        print("No cameras opened. Start DroidCam Client and activate the phone first.")


if __name__ == "__main__":
    main()
