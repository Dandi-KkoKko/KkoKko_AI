import io
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MODEL = Path(__file__).parent / "models" / "scrfd_10g.onnx"
MAX_PIXELS = 50_000_000  # 압축 폭탄 방지: 디코딩 전에 헤더로 확인
MAX_SIDE = 1920  # 검사 크기
STORE_SIDE = 1080  # 저장 크기: 검사한 이미지를 줄인 것이라 얼굴 좌표 비율은 그대로
SMALL_SIDE = 320  # 화면을 꽉 채운 큰 얼굴은 원본 크기에서 놓치므로 축소본으로 한 번 더 검사
SCORE_THRESHOLD = 0.5


class InvalidImage(ValueError):
    pass


# Windows에서 OpenCV가 한글 경로를 못 열어서 바이트로 로드
_net = cv2.dnn.readNetFromONNX(np.frombuffer(MODEL.read_bytes(), np.uint8))
_outputs = _net.getUnconnectedOutLayersNames()
_lock = threading.Lock()


def _detect(img):
    h, w = img.shape[:2]
    H, W = -(-h // 32) * 32, -(-w // 32) * 32
    padded = cv2.copyMakeBorder(img, 0, H - h, 0, W - w, cv2.BORDER_CONSTANT)
    blob = cv2.dnn.blobFromImage(padded, 1 / 128, (W, H), (127.5,) * 3, swapRB=True)
    with _lock:
        _net.setInput(blob)
        outs = _net.forward(_outputs)  # 점수 8/16/32, 박스 8/16/32, 랜드마크 8/16/32
    faces = []
    for i, s in enumerate((8, 16, 32)):
        ys, xs = np.mgrid[: H // s, : W // s]
        centers = np.repeat(np.stack([xs.ravel(), ys.ravel()], 1) * s, 2, axis=0)  # 칸마다 앵커 2개
        scores, dist = outs[i][:, 0], outs[i + 3] * s
        k = scores >= SCORE_THRESHOLD
        p1, p2 = centers[k] - dist[k, :2], centers[k] + dist[k, 2:]
        faces += [(a[0] / w, a[1] / h, (b[0] - a[0]) / w, (b[1] - a[1]) / h, sc) for a, b, sc in zip(p1, p2, scores[k])]
    return faces


def _shrink(img, side):
    s = side / max(img.shape[:2])
    return cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img


def check(data: bytes):
    try:
        w, h = Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]).size
    except Exception as e:
        raise InvalidImage("JPEG/PNG/WebP 이미지가 아님") from e
    if w * h > MAX_PIXELS:
        raise InvalidImage("픽셀 수 초과")
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as e:
        raise InvalidImage("디코딩 실패") from e
    if img is None:
        raise InvalidImage("디코딩 실패")

    img = _shrink(img, MAX_SIDE)
    boxes = _detect(img) + _detect(_shrink(img, SMALL_SIDE))
    keep = cv2.dnn.NMSBoxes([b[:4] for b in boxes], [b[4] for b in boxes], SCORE_THRESHOLD, 0.3) if boxes else []
    faces = [dict(zip(("x", "y", "width", "height", "score"), (round(float(v), 4) for v in boxes[i]))) for i in keep]
    return cv2.imencode(".jpg", _shrink(img, STORE_SIDE), [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes(), faces


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="사진을 검사해 BLOCK(얼굴 감지)/PASS 출력")
    ap.add_argument("paths", nargs="+", type=Path, help="사진 파일 또는 폴더")
    ap.add_argument("--threshold", type=float, default=SCORE_THRESHOLD)
    ap.add_argument("--out", type=Path, help="얼굴 박스를 그린 검사 이미지를 저장할 폴더")
    args = ap.parse_args()
    SCORE_THRESHOLD = args.threshold
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    files = [f for p in args.paths for f in (sorted(p.iterdir()) if p.is_dir() else [p]) if f.is_file()]
    blocked = 0
    for f in files:
        t = time.perf_counter()
        try:
            jpeg, faces = check(f.read_bytes())
        except InvalidImage as e:
            print(f"INVALID {f.name}: {e}")
            continue
        ms = (time.perf_counter() - t) * 1000
        blocked += bool(faces)
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        det_w = w * min(MAX_SIDE, max(Image.open(f).size)) / max(w, h)  # 얼굴 폭은 검사 이미지 기준으로 출력
        print(f"{'BLOCK' if faces else 'PASS '} {f.name} (저장 {w}x{h}, {ms:.0f}ms) "
              + " ".join(f"{x['score']:.2f}/{x['width'] * det_w:.0f}px" for x in faces))
        if args.out:
            for x in faces:
                p1 = (int(x["x"] * w), int(x["y"] * h))
                p2 = (int((x["x"] + x["width"]) * w), int((x["y"] + x["height"]) * h))
                cv2.rectangle(img, p1, p2, (0, 0, 255), 2)
                cv2.putText(img, f"{x['score']:.2f}", (p1[0], p1[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            (args.out / f"{f.stem}.jpg").write_bytes(cv2.imencode(".jpg", img)[1].tobytes())
    print(f"{blocked}/{len(files)} blocked")
