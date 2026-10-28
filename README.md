# KkoKko_AI

# 가까이 사진 검사 서버

사진을 비공개로 접수한 뒤 SCRFD-10G로 얼굴을 검사하는 Python 서버다. 검사 중인 기록은 작성자에게만 보이고, **검사 통과 + 접수 후 2시간 경과**를 만족하면 공개된다. 얼굴이 감지되면 게시를 거절하고 결과 알림을 저장한다.

React Native/Expo 화면은 이 저장소에 포함되지 않는다. 프론트와 기존 로그인 서버가 아래 API에 연결한다. 제품 요구사항은 [PRD](PRD.md)를 참고한다.

## 현재 구현

- SCRFD-10G, 임계값 0.5, 검사 긴 변 최대 1920px 및 큰 얼굴용 320px 추가 검사.
- 통과 이미지의 저장 크기: 긴 변 최대 1080px, JPEG 품질 90.
- SQLite에 기록·작업 상태·알림 저장. 작업자 재시작 시 중단 작업 복구.
- 본인 전용 조회, 공개 시각 제한, 중복 요청 방지, 검사 오류 재시도.
- 감지 테스트 7개와 API·작업자 테스트 14개: 기존 로컬 검증 통과.

실제 로그인, 프론트 화면, OS 푸시, 거리별 사진 잠금 해제, AI 태그 생성, AWS 배포는 별도 연결 대상이다. 현재 공개 사진의 이미지 API에는 거리 제한이 없다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| [face_check.py](face_check.py) | 사진 검증·방향 보정·얼굴 감지·저장 JPEG 생성 |
| [post_api.py](post_api.py) | 업로드·기록·이미지·알림 API, 테스트 인증 |
| [post_worker.py](post_worker.py) | 단일 검사 작업자, 모델 프로세스 재사용·시간 초과 처리 |
| [post_store.py](post_store.py) | SQLite 저장·상태 변경·공개 판단·파일 정리 |
| [test_post_api.py](test_post_api.py) | API 권한·상태·복구·실제 SCRFD 연결 테스트 |
| [test_face_check.py](test_face_check.py) | 얼굴 감지·이미지 처리 테스트 |
| [compare_models.py](compare_models.py) | YuNet/SCRFD 비교 도구 |
| [benchmark_limited.py](benchmark_limited.py) | Windows CPU·메모리 제한 측정 도구 |
| [requirements.txt](requirements.txt) | 서버 패키지 버전 |
| `models/scrfd_10g.onnx` | 서버에서 사용하는 모델, 약 17MB |
| `models/face_detection_yunet_2026may.onnx` | 비교용 YuNet 모델 |
| `private_data/` | 기본 DB·비공개 이미지 저장 폴더, 실행 시 생성, Git 제외 |

## 실행 준비

Python 3.11 기준으로 개발·검증했다. 프로젝트 루트에서 가상환경을 만들고 실행한다. GPU는 필요 없다. 모델 파일도 서버로 함께 배포해야 하며, 첫 실행 시 자동 다운로드하지 않는다.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Ubuntu

```bash
sudo apt-get update
sudo apt-get install -y python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

서버용 `opencv-python-headless`를 사용한다. 같은 환경에 `opencv-python`과 `opencv-python-headless`를 중복 설치하지 말고 새 가상환경을 권장한다. Ubuntu의 Python 버전과 대상 서버에서 패키지 설치·검사 실행을 확인한다.

## 로컬 실행

API와 작업자는 **같은 프로젝트·같은 데이터 폴더**를 사용하고 각각 별도 터미널에서 실행한다. 아래 예시는 기본 `private_data/`를 사용한다.

### Windows

터미널 A에서 개발용 토큰을 생성하고 API를 실행한다. 출력되는 토큰은 `/docs`의 Authorize 또는 테스트 요청에 사용한다.

```powershell
$token = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:FACE_API_TOKENS = @{$token = 'demo-user-1'} | ConvertTo-Json -Compress
$token
.\.venv\Scripts\python.exe -m uvicorn post_api:app --host 127.0.0.1 --port 8000 --workers 1
```

터미널 B에서도 프로젝트 루트로 이동한 뒤 실행한다.

```powershell
.\.venv\Scripts\python.exe post_worker.py
```

### Ubuntu

터미널 A:

```bash
export FACE_API_TOKENS="$(.venv/bin/python -c 'import json,secrets; print(json.dumps({secrets.token_urlsafe(32): "demo-user-1"}))')"
printf '%s\n' "$FACE_API_TOKENS"
.venv/bin/python -m uvicorn post_api:app --host 127.0.0.1 --port 8000 --workers 1
```

터미널 B:

```bash
.venv/bin/python post_worker.py
```

생성한 토큰은 개발용 비밀값이다. 저장소나 프론트 소스에 커밋하지 않는다. API를 재실행할 때 새 토큰을 만들면 이전 토큰은 더 이상 사용할 수 없다.

- API 문서: [Swagger UI](http://127.0.0.1:8000/docs)
- 기계 판독 명세: [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)
- API 응답 확인: [health](http://127.0.0.1:8000/health)

`/health`는 API 응답 여부만 확인한다. 작업자·모델이 정상인지 검사하지 않으므로, 실제 사진의 상태가 `pending`에서 바뀌는지도 확인해야 한다.

## 환경변수

| 변수 | 기본값 | 적용 대상과 의미 |
|---|---|---|
| `FACE_DATA_DIR` | 프로젝트의 `private_data/` | API·작업자 모두 같은 절대 경로 사용 |
| `FACE_API_TOKENS` | `{}` | API의 개발용 `{"토큰":"사용자 ID"}` 매핑. 비어 있으면 인증 API는 401 |
| `FACE_PUBLIC_DELAY_SECONDS` | `7200` | API 접수 시 적용할 공개 대기 시간. 0~86400초 |
| `FACE_CORS_ORIGINS` | 비어 있음 | 브라우저 프론트의 허용 origin, 쉼표로 구분 |

환경변수는 프로세스 시작 전에 설정한다. `.env` 파일을 자동으로 읽는 코드는 없다. 데이터 폴더를 지정할 때는 API와 작업자 터미널 모두에 같은 값을 설정한다.

예를 들어 즉시 공개 시연은 API 실행 전에 `FACE_PUBLIC_DELAY_SECONDS=0`을 설정한다. 이미 접수한 기록의 `publish_at`은 변경되지 않는다.

모델 경로·검사 크기·저장 크기·임계값은 `face_check.py`의 상수다. 현재 작업자는 1920px / 1080px / 0.5 설정을 확인하므로, 이후 변경하려면 작업자와 테스트도 함께 수정해야 한다.

## 기존 로그인 서버 연결

개발용 매핑은 실제 로그인 기능이 아니다. 운영에서는 [post_api.py](post_api.py)의 `current_user` 의존성을 기존 서버가 검증한 사용자 ID를 반환하는 함수로 교체하거나 `app.dependency_overrides`로 연결한다.

연결 함수는 다음 계약을 지켜야 한다.

- 로그인 토큰의 유효성을 서버에서 검증하고 안정적인 사용자 ID 문자열을 반환한다.
- 미인증 요청은 401로 거절한다.
- 프론트가 임의로 보낸 `owner`, `user_id`, 승인 플래그를 소유권이나 승인 근거로 사용하지 않는다.
- 모든 보호 API에 `Authorization: Bearer <로그인 토큰>`을 보낸다. 이미지 요청에도 같은 헤더가 필요하다.

기존 백엔드가 Python이 아니라면 이 API를 별도 서비스로 실행하고 HTTP로 연동할 수 있다. 이 경우에도 토큰 검증 또는 신뢰할 수 있는 서버 간 인증 규약을 먼저 연결해야 한다. 임의의 사용자 ID 헤더를 신뢰하도록 바꾸면 본인 전용 접근 제어가 깨진다.

## API 목록

| 메서드·경로 | 인증 | 기능 |
|---|---|---|
| `POST /posts` | 필요 | 사진 접수, `202 Accepted` 반환 |
| `GET /posts/mine` | 필요 | 내 기록 전체 상태 조회 |
| `GET /posts` | 불필요 | 공개된 기록 목록 조회 |
| `GET /posts/{id}` | 필요 | 본인 기록 또는 공개된 기록의 상세·상태 조회 |
| `GET /posts/{id}/image` | 필요 | 소유권·공개 여부에 따라 이미지 제공 |
| `POST /posts/{id}/retry` | 필요 | 본인 검사 실패 기록 재시도 |
| `GET /notifications` | 필요 | 본인 검사 결과 알림 조회 |
| `POST /notifications/{id}/read` | 필요 | 본인 알림 읽음 처리 |
| `GET /health` | 불필요 | API 응답 확인 |

목록 API는 `limit=50`, `offset=0`이 기본값이고 `limit`은 최대 100이다. 알림은 기본 `unread_only=true`이며, 읽은 알림까지 보려면 `false`를 보낸다. 내 기록 목록의 `total`에는 거절·실패 기록도 포함된다. 공개 목록에는 작성자 ID를 반환하지 않으며, 현재 위치 범위나 해시태그 검색 필터는 없다.

### 사진 접수

`POST /posts`의 본문은 `multipart/form-data`다. 필드는 `image`와 `metadata` 두 개이며, `metadata`는 아래 구조의 **JSON 문자열**이다. multipart의 `Content-Type`과 boundary는 사용하는 HTTP 라이브러리가 설정하게 한다.

```json
{
  "client_request_id": "9d8b405a-d128-41b7-b4c5-fc95ac5bad00",
  "title": "골목",
  "caption": "조용한 오후",
  "latitude": 35.87,
  "longitude": 128.6,
  "hashtags": ["산책"]
}
```

| 필드 | 규칙 |
|---|---|
| `image` | JPEG/PNG/WebP 정지 이미지 한 장, 최대 10MiB, 최대 16,000,000픽셀 |
| `client_request_id` | 필수 UUID, 동일 요청의 재전송 시 재사용 |
| `title` | 필수, 공백 정리 후 1~10자 |
| `caption` | 필수, 공백 정리 후 1~40자 |
| `latitude` / `longitude` | 필수, 각각 -90~90 / -180~180 범위의 유한한 수 |
| `hashtags` | 생략 가능, 최대 3개, 각 1~30자, 중복 불가 |

정의되지 않은 metadata 필드는 거절한다. `neighborhood_id`를 받던 초기 인수인계 초안과 달리 현재 계약은 위도·경도를 사용한다. 프론트는 전송 전에 긴 변 최대 1920px로 줄이고, HEIC 사진은 JPEG 등 허용 형식으로 변환한다. 사진을 줄여도 제목·위치 등 별도 입력은 metadata로 전달해야 한다.

동일 사용자·동일 `client_request_id`·동일 사진 및 입력값을 다시 보내면 기존 기록을 반환한다. 같은 UUID로 사진 또는 입력값을 바꾸면 `409 IDEMPOTENCY_CONFLICT`다. 전송 실패 뒤 재전송할 수 있도록 UUID와 전송할 파일을 보관하고, 새 사진이나 내용으로 바꾸면 새 UUID를 생성한다.

### 접수 응답 예시

아래 ID와 시각은 응답 형식을 설명하기 위한 예시다.

```json
{
  "id": "9d8b405ad12841b7b4c5fc95ac5bad01",
  "title": "골목",
  "caption": "조용한 오후",
  "latitude": 35.87,
  "longitude": 128.6,
  "hashtags": ["산책"],
  "status": "pending",
  "visibility": "owner_only",
  "message": "사진 확인을 기다리고 있어요. 나에게만 보여요.",
  "reason_code": null,
  "created_at": 1800000000.0,
  "publish_at": 1800007200.0,
  "image_url": "/posts/9d8b405ad12841b7b4c5fc95ac5bad01/image",
  "can_retry": false,
  "poll_after_seconds": 3
}
```

`202`는 접수 성공이며 공개 승인이 아니다. `image_url`은 API 기준 상대 경로이고, 클라이언트가 base URL과 결합해야 한다. 거절 상태에서는 `image_url=null`이다. 모든 시각은 UTC Unix **초**이므로 JavaScript `Date` 등에 넣을 때 밀리초로 변환한다.

### 상태와 프론트 처리

| `status` | 화면 처리 | `visibility` | 다음 조회 |
|---|---|---|---|
| `pending` | 검사 대기 · 나에게만 표시 | `owner_only` | 3초 |
| `processing` | 검사 중 · 나에게만 표시 | `owner_only` | 3초 |
| `scheduled` | 검사 통과 · 공개 예정 시각 표시 | `owner_only` | 60초 |
| `published` | 공개 완료 | `public` | 상태 폴링 종료 |
| `rejected` | 사유 표시, 다른 사진으로 새 접수 | `owner_only` | 상태 폴링 종료 |
| `failed` | 오류 표시, 재시도 버튼 | `owner_only` | 사용자 재시도 시 재개 |

일반 흐름은 `pending → processing → scheduled → published`다. 검사 완료가 공개 예정 시각보다 늦으면 `processing → published`가 될 수 있다. DB의 `approved`는 내부 값이며 API 상태로 직접 반환하지 않는다.

`publish_at`은 접수 시 정한 공개 가능 시각이지 실제 공개 완료 시각이 아니다. 시각이 지나도 검사 통과 전에는 공개하지 않는다. 앱이 백그라운드일 때 폴링을 중단했다면 복귀 시 기록과 알림을 다시 조회한다.

### 알림과 재시도

`GET /notifications`는 `{ "items": [...] }` 형태다. 각 항목은 `id`, `post_id`, `kind`, `message`, `created_at`, `read_at`을 포함한다. 프론트는 `id`로 중복 표시를 막고, 사용자가 확인하면 읽음 API를 호출한다.

`kind`는 `scheduled`, `published`, `FACE_DETECTED`, `INVALID_IMAGE`, `DETECTION_TIMEOUT`, `DETECTION_UNAVAILABLE`, `EXPIRED` 중 하나다. 검사 통과 시점의 알림만 생성하며, 2시간이 지나 `scheduled`에서 공개로 바뀔 때 별도 알림을 추가하지 않는다. 휴대폰 OS 푸시는 전송하지 않는다.

`POST /posts/{id}/retry`는 본인의 `failed` 기록을 다시 `pending`으로 바꾸고 이전 알림을 읽음 처리한다. 이미 `pending`·`processing`인 기록은 그대로 반환한다. 거절·공개 기록 또는 원본이 없는 기록은 `409 REUPLOAD_REQUIRED`이며 새 사진 접수가 필요하다.

### 오류 처리

접수·조회 오류 응답의 기본 형태는 `{ "code": "...", "message": "..." }`다.

| HTTP | 코드 | 의미 |
|---|---|---|
| 400 | `INVALID_IMAGE` | 허용하지 않는 형식, 이미지 헤더 오류, 애니메이션 이미지 |
| 401 | `UNAUTHORIZED` | 인증 필요 또는 토큰 불일치 |
| 404 | `NOT_FOUND` | 기록 없음, 타인의 비공개 기록, 접근할 수 없는 이미지·알림 |
| 408 | `UPLOAD_TIMEOUT` | 업로드 본문 수신 시간 초과 |
| 409 | `IDEMPOTENCY_CONFLICT` | 같은 UUID에 다른 사진·입력값 사용 |
| 409 | `REUPLOAD_REQUIRED` | 해당 기록은 검사 재시도 불가 |
| 413 | `IMAGE_TOO_LARGE` | 파일 용량·전체 요청 용량·픽셀 상한 초과 |
| 422 | `INVALID_METADATA`, `INVALID_REQUEST` | 필수 필드·값·UUID·길이 규칙 오류 |
| 429 | `SERVER_BUSY`, `QUEUE_FULL` | 동시 전송 또는 대기량 제한 |

접수 후 검사 실패는 위 HTTP 오류와 구분한다. 상태 조회는 정상 응답을 반환하면서 `status=failed`와 `reason_code`를 제공한다. 얼굴이 발견되면 `status=rejected`, `reason_code=FACE_DETECTED`다. 헤더 검증은 통과했지만 디코딩에 실패한 파일도 비동기 검사에서 `rejected/INVALID_IMAGE`가 될 수 있다.

## 호출 예시

아래 예시는 추가로 `httpx`가 필요하다. 가상환경의 Python에서 `python -m pip install httpx==0.28.1`로 설치한다. API와 작업자를 실행한 뒤 프로젝트 루트에서 사용한다. `demo-user-1`에 발급한 토큰을 입력하면 기본 테스트 사진을 접수하고 내 기록을 조회한다.

```python
import getpass
import json
from pathlib import Path
from uuid import uuid4

import httpx

token = getpass.getpass("개발용 Bearer 토큰: ")
metadata = {
    "client_request_id": str(uuid4()),
    "title": "골목",
    "caption": "조용한 오후",
    "latitude": 35.87,
    "longitude": 128.60,
    "hashtags": ["산책"],
}
with httpx.Client(
    base_url="http://127.0.0.1:8000",
    headers={"Authorization": f"Bearer {token}"},
    timeout=60,
) as client:
    with Path("tests/building.jpg").open("rb") as photo:
        response = client.post(
            "/posts",
            files={"image": ("building.jpg", photo, "image/jpeg")},
            data={"metadata": json.dumps(metadata, ensure_ascii=False)},
        )
    response.raise_for_status()
    post = response.json()
    print("접수:", post)
    print("내 기록:", client.get("/posts/mine").json())
    print("상태 조회 경로:", f"/posts/{post['id']}")
```

## 저장과 작업자 운영

```text
프론트 / 기존 백엔드
       │ multipart 요청 · 상태 조회
       ▼
FastAPI API ── SQLite 기록·대기 상태 / 비공개 이미지 폴더
                         ▲
                         │ 작업 가져오기 · 결과 저장
                  단일 검사 작업자
                         │
                  SCRFD 모델 자식 프로세스
```

API는 모델을 로드하지 않는다. 검사 작업자가 별도 모델 프로세스를 처음 작업할 때 준비하고 이후 재사용한다. 시간 초과·프로세스 장애가 발생하면 해당 모델 프로세스를 정리하고 다음 작업에서 새로 준비한다.

- API는 `--workers 1`로 실행한다. 업로드 동시 제한은 API 프로세스 단위다.
- 검사 작업자는 같은 데이터 폴더에서 하나만 실행할 수 있다. 파일 잠금으로 중복 실행을 막는다.
- API·작업자·모델 자식 프로세스가 모두 메모리를 사용하므로 모델 단독 측정값만으로 서버 여유를 판단하지 않는다.
- 작업자는 한 번에 한 장만 검사한다. 대기 사진은 디스크에 두며 모두 메모리에 올리지 않는다.
- `pending`·`processing`·`failed` 합계 사용자당 3개, 전체 50개까지 유지한다. 한도에 도달하면 새 접수를 거절한다.
- 업로드 파일은 최대 10MiB, multipart 전체 요청은 최대 10MiB + 256KiB다. 본문 수신에는 전체 약 60초 및 청크 대기 최대 30초 제한을 적용한다.
- API는 최대 16MP 입력을 받는다. 감지 모듈을 직접 호출할 때의 `MAX_PIXELS=50MP`보다 제한적이다.
- 모델 초기화 대기 상한 60초, 개별 검사 응답 대기 상한 45초다. 타임아웃은 게시 승인으로 처리하지 않는다.

사진은 공개 전후 모두 같은 비공개 폴더에 보관하고, API가 DB 상태를 확인해 접근을 허용한다. `private_data/`를 웹 서버의 정적 파일 경로로 노출하면 안 된다. API 응답은 `private, no-store` 캐시 정책을 사용한다.

통과하면 저장 JPEG를 남기고 업로드 원본을 정리한다. 얼굴 감지·유효하지 않은 이미지 거절 시 원본을 정리한다. `failed` 원본은 재시도용으로 유지한다. 접수 후 24시간이 지난 `pending`·`failed`는 `rejected/EXPIRED`로 변경하고 사진을 정리한다. 이 정리는 작업자 시작 시와 실행 중 약 60초 간격으로 수행하므로 작업자가 멈추면 삭제도 지연된다.

DB에 연결되지 않은 업로드 잔여 파일은 생성 후 1시간이 지나면 정리 대상이 된다. 게시 이미지, 기록 메타데이터, 알림을 장기적으로 삭제하는 정책과 삭제 API는 아직 없다.

## 검증

가상환경의 Python으로 실행한다. API 테스트의 `httpx`는 서버 실행 의존성과 별도로 설치한다.

```bash
python -m pip install httpx==0.28.1
python test_face_check.py
python -m unittest test_post_api -v
```

두 명령은 순차 실행한다. 로컬 검증 범위는 감지 7개, API·작업자 14개이며, 실제 SCRFD 추론, 타인 접근 차단, 공개 시간 제한, 중복 요청, 검사 오류·시간 초과, 작업자 재시작 복구를 포함한다. 테스트는 임시 데이터 폴더를 사용한다.

개별 사진 검사:

```bash
python face_check.py tests/building.jpg tests/astronaut.png
```

`face_check.py` CLI는 감지 확인 도구다. 비공개 접수·인증·2시간 공개 지연·알림 처리는 API와 작업자가 담당한다.

## AWS 연결 전 확인

현재 서버 후보는 `t2.micro`이고 실제 AWS 측정·배포는 아직 수행하지 않았다. 먼저 SCRFD 1920px 구성으로 메모리와 연속 요청 지연을 확인한다. CPU 크레딧 소진에 따른 성능 변화와 무료 혜택 적용 여부는 실제 계정·인스턴스에서 확인해야 한다.

서버 연결 시 필요한 작업은 실제 로그인 연결, HTTPS 및 접근 설정, API·작업자의 서비스 등록과 자동 재시작, 동일 영구 디스크 경로 설정이다. 개발 예시의 `127.0.0.1`은 로컬 접근용이다. 외부 접속 구성에서는 프록시와 바인딩 주소를 맞추고 HTTPS로 인증 토큰을 전달한다. 이 저장소에는 AWS 자동 배포 설정이 없다.

| 증상 | 확인할 내용 |
|---|---|
| 모든 요청이 401 | 토큰 환경변수, Authorization 헤더, 실제 로그인 연결 |
| 상태가 계속 pending | 작업자 실행 여부, API와 작업자의 `FACE_DATA_DIR` 일치 여부 |
| 통과했는데 공개 목록에 없음 | `scheduled` 여부와 `publish_at`, 기본 2시간 규칙 |
| 이미지 요청만 실패 | 이미지 요청에도 인증 헤더를 전달했는지 |
| 작업자가 중복 실행 오류로 종료 | 같은 데이터 폴더를 사용하는 기존 작업자 확인 |
| 모듈 import 실패 | 가상환경, requirements 설치, 모델 파일 포함 여부 |
| API 테스트에서 httpx 오류 | 테스트용 `httpx==0.28.1` 설치 여부 |

## 모델 이용 조건과 이전 기록

SCRFD 코드와 공개 가중치는 이용 조건이 다르다. [InsightFace 공식 안내](https://github.com/deepinsight/insightface#license)는 공개 모델을 비상업 연구용으로 제한한다. 파인튜닝 또는 해커톤 사용만으로 이 조건이 없어지지는 않는다. 모델 사용 범위는 출시 전에 확인한다.

기존 [HANDOFF.md](HANDOFF.md)와 [HANDOFF_FACE_DETECTION.md](HANDOFF_FACE_DETECTION.md)는 기획·실험 이력이다. 현재 API는 `202` 비동기 접수 방식이며, 초기 문서의 동기식 `201/422/503` 게시 결과 계약과 다르다. 현재 상태는 이 README와 코드, `/openapi.json`을 기준으로 확인한다.
