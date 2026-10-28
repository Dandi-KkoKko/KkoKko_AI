import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from face_check import InvalidImage, check

T = Path(__file__).parent / "tests"
astronaut = cv2.imdecode(np.fromfile(T / "astronaut.png", np.uint8), cv2.IMREAD_COLOR)  # 얼굴: x178 y63 90x113


def jpg(img):
    return cv2.imencode(".jpg", img)[1].tobytes()


def decode(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def invalid(data):
    try:
        check(data)
    except InvalidImage:
        return True
    return False


def test_face_blocked():
    out, faces = check((T / "astronaut.png").read_bytes())
    assert len(faces) == 1 and abs(faces[0]["x"] - 178 / 512) < 0.02
    assert decode(out).shape == astronaut.shape


def test_no_face_passes():
    assert check((T / "building.jpg").read_bytes())[1] == []


def test_close_up_selfie_blocked():
    # 화면을 꽉 채운 얼굴: 원본 크기 검사만으로는 놓치고 320px 축소본에서 잡힌다
    big = cv2.resize(astronaut[60:180, 175:270], (1425, 1800))
    assert check(jpg(big))[1]


def test_small_background_face_blocked():
    # 건물 사진 속 16px 얼굴: YuNet은 못 잡고 SCRFD는 잡는다
    img = cv2.resize(decode((T / "building.jpg").read_bytes()), (1920, 1327))
    head = cv2.resize(astronaut[20:220, 140:320], None, fx=16 / 90, fy=16 / 90, interpolation=cv2.INTER_AREA)
    img[900:900 + head.shape[0], 300:300 + head.shape[1]] = head
    assert check(jpg(img))[1]


def test_exif_rotation_applied_and_stripped():
    upright = astronaut[:400, 100:400]
    raw = jpg(cv2.rotate(upright, cv2.ROTATE_90_COUNTERCLOCKWISE))
    exif = b"Exif\0\0MM\0\x2a\0\0\0\x08\0\x01\x01\x12\0\x03\0\0\0\x01\0\x06\0\0\0\0\0\0"  # Orientation=6
    raw = raw[:2] + b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif + raw[2:]
    out, faces = check(raw)
    assert faces and decode(out).shape == upright.shape and b"Exif" not in out


def test_large_photo_shrunk():
    out, _ = check(jpg(cv2.resize(astronaut, (4000, 3000))))
    assert decode(out).shape[:2] == (810, 1080)


def test_bad_input_rejected():
    assert invalid(b"not an image")
    assert invalid(cv2.imencode(".bmp", astronaut)[1].tobytes())
    assert invalid((T / "building.jpg").read_bytes()[:200])
    bomb = io.BytesIO()
    Image.new("1", (10_000, 6_000)).save(bomb, "PNG")  # 60MP인데 파일은 작음
    assert invalid(bomb.getvalue())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
