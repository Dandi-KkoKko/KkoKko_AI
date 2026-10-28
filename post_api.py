"""Run: python -m uvicorn post_api:app --host 0.0.0.0 --port 8000 --workers 1.

Run post_worker.py separately with the same FACE_DATA_DIR. Interactive API: /docs.
FACE_API_TOKENS is a JSON object mapping development Bearer tokens to user IDs.
Production: override current_user with the existing server's verified login dependency.
Never accept an owner ID directly from a client. FACE_PUBLIC_DELAY_SECONDS defaults
to 7200, matching Figma's location-protection rule; set 0 for immediate demo publishing.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from typing import Annotated, Literal
from uuid import UUID, uuid4
import warnings

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from post_store import DATA_DIR, Store, StoreError, public, representation

MAX_FILE_BYTES = 10 * 1024**2
MAX_BODY_BYTES = MAX_FILE_BYTES + 256 * 1024
MAX_INPUT_PIXELS = 16_000_000
bearer = HTTPBearer(auto_error=False)


class RecordFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=10)
    caption: str = Field(min_length=1, max_length=40)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    hashtags: list[Annotated[str, Field(min_length=1, max_length=30)]] = Field(default_factory=list, max_length=3)

    @field_validator("hashtags")
    @classmethod
    def unique_tags(cls, tags):
        if len(set(tags)) != len(tags):
            raise ValueError("중복 해시태그는 사용할 수 없습니다")
        return tags


class PostInput(RecordFields):
    client_request_id: UUID


class PostOutput(RecordFields):
    id: str
    status: Literal["pending", "processing", "scheduled", "published", "rejected", "failed"]
    visibility: Literal["public", "owner_only"]
    message: str
    reason_code: str | None
    created_at: float
    publish_at: float
    image_url: str | None
    can_retry: bool
    poll_after_seconds: int | None


class PostList(BaseModel):
    items: list[PostOutput]


class MyPosts(PostList):
    total: int


class Notification(BaseModel):
    id: str
    post_id: str
    kind: str
    message: str
    created_at: float
    read_at: float | None


class NotificationList(BaseModel):
    items: list[Notification]


def fail(status, code, message):
    raise HTTPException(status, {"code": code, "message": message})


def current_user(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if credentials:
        for token, user in request.app.state.tokens.items():
            if hmac.compare_digest(token.encode(), credentials.credentials.encode()):
                return user
    fail(401, "UNAUTHORIZED", "로그인이 필요해요.")


class UploadLimits:
    def __init__(self, app):
        self.app, self.active = app, 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/posts":
            return await self.app(scope, receive, send)
        if self.active >= 2:
            return await JSONResponse({"code": "SERVER_BUSY", "message": "잠시 후 다시 시도해주세요."}, 429)(scope, receive, send)
        self.active += 1
        received = 0
        deadline = asyncio.get_running_loop().time() + 60

        async def limited_receive():
            nonlocal received
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                message = await asyncio.wait_for(receive(), max(0, min(30, remaining)))
            except asyncio.TimeoutError:
                fail(408, "UPLOAD_TIMEOUT", "사진 전송 시간이 초과됐어요.")
            received += len(message.get("body", b""))
            if received > MAX_BODY_BYTES:
                fail(413, "IMAGE_TOO_LARGE", "사진은 10MiB 이하로 올려주세요.")
            return message

        try:
            await self.app(scope, limited_receive, send)
        finally:
            self.active -= 1


def create_app(root=DATA_DIR, tokens=None, public_delay=None):
    app = FastAPI(title="가까이 — 사진 검사·공개 API", version="1.0.0",
                  description="촬영 → 제목/코멘트/태그 → 위치 확인 → 접수. pending/processing은 본인만 조회. "
                              "검사 통과 후 scheduled, 공개 시각 경과 후 published. 시간은 UTC Unix 초. "
                              "알림은 조회/읽음 API로 전달하며 OS 푸시는 포함하지 않습니다.")
    store = Store(root)
    app.state.store = store
    app.state.tokens = tokens if tokens is not None else json.loads(os.environ.get("FACE_API_TOKENS", "{}"))
    delay = float(public_delay if public_delay is not None else os.environ.get("FACE_PUBLIC_DELAY_SECONDS", 7200))
    if not 0 <= delay <= 86400:
        raise ValueError("public delay must be between 0 and 86400 seconds")
    app.add_middleware(UploadLimits)
    origins = [s.strip() for s in os.environ.get("FACE_CORS_ORIGINS", "").split(",") if s.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def no_shared_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        detail = error.detail if isinstance(error.detail, dict) else {"code": "REQUEST_ERROR", "message": str(error.detail)}
        return JSONResponse(detail, error.status_code)

    @app.exception_handler(StoreError)
    async def store_error(request, error):
        return JSONResponse({"code": error.code, "message": "요청 상태를 확인하고 다시 시도해주세요."}, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"code": "INVALID_REQUEST", "message": "필수 입력값을 확인해주세요."}, 422)

    def visible_row(post_id, user):
        row = store.get(post_id)
        if not row or (row["owner"] != user and not public(row)):
            fail(404, "NOT_FOUND", "기록을 찾을 수 없어요.")
        return row

    @app.get("/health", tags=["system"])
    def health():
        return {"api": "ok", "worker": "run post_worker.py separately"}

    @app.post("/posts", status_code=202, response_model=PostOutput, tags=["records"], summary="위치 확인 완료 후 사진 접수",
              description="multipart/form-data로 image와 metadata(JSON 문자열)를 전송합니다. client_request_id는 프론트에서 생성한 UUID이며 통신 재시도에는 같은 값을 사용합니다. 사진이나 입력값을 변경하면 새 UUID를 사용하세요. 접수 성공은 공개 승인이 아닙니다.")
    def submit(image: Annotated[UploadFile, File(description="JPEG/PNG/WebP, 10MiB 이하, 정지 이미지 1장")],
               metadata: Annotated[str, Form(description="PostInput JSON: client_request_id(UUID), title(10), caption(40), latitude, longitude, hashtags(최대3)",
                    examples=['{"client_request_id":"9d8b405a-d128-41b7-b4c5-fc95ac5bad00","title":"골목","caption":"조용한 오후","latitude":35.87,"longitude":128.60,"hashtags":["산책"]}'])],
               user: Annotated[str, Depends(current_user)]):
        if len(metadata) > 4096:
            fail(422, "INVALID_METADATA", "입력값이 너무 길어요.")
        try:
            fields = PostInput.model_validate_json(metadata)
        except ValidationError:
            fail(422, "INVALID_METADATA", "제목 1~10자, 코멘트 1~40자, 태그 최대 3개와 위치를 확인해주세요.")
        post_id = uuid4().hex
        path = store.path(post_id)
        data = fields.model_dump(mode="json", exclude={"client_request_id"})
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode())
        keep = False
        try:
            size = 0
            with path.open("xb") as output:
                while chunk := image.file.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        fail(413, "IMAGE_TOO_LARGE", "사진은 10MiB 이하로 올려주세요.")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(path, formats=["JPEG", "PNG", "WEBP"]) as probe:
                        if probe.width * probe.height > MAX_INPUT_PIXELS:
                            fail(413, "IMAGE_TOO_LARGE", "사진을 긴 변 1920px로 줄여서 올려주세요.")
                        if getattr(probe, "n_frames", 1) != 1:
                            fail(400, "INVALID_IMAGE", "정지 이미지 한 장만 올릴 수 있어요.")
                        mime = Image.MIME[probe.format]
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                fail(400, "INVALID_IMAGE", "JPEG/PNG/WebP 사진을 선택해주세요.")
            row = store.submit(post_id, user, str(fields.client_request_id), digest.hexdigest(), data, mime, delay)
            keep = row["id"] == post_id
            return representation(row)
        finally:
            if not keep:
                path.unlink(missing_ok=True)
            image.file.close()

    @app.get("/posts/mine", response_model=MyPosts, tags=["records"], summary="내 기록: 검사 중·거절·실패 포함")
    def mine(user: Annotated[str, Depends(current_user)], limit: int = Query(50, ge=1, le=100),
             offset: int = Query(0, ge=0)):
        with store.connect() as db:
            rows = db.execute("SELECT * FROM posts WHERE owner=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (user, limit, offset)).fetchall()
            total = db.execute("SELECT count(*) FROM posts WHERE owner=?", (user,)).fetchone()[0]
        return {"items": [representation(r) for r in rows], "total": total}

    @app.get("/posts", response_model=PostList, tags=["records"], summary="다른 사용자 지도·리스트: 공개된 기록만")
    def published(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        with store.connect() as db:
            rows = db.execute("SELECT * FROM posts WHERE status='approved' AND publish_at<=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (time.time(), limit, offset)).fetchall()
        return {"items": [representation(r) for r in rows]}

    @app.get("/posts/{post_id}", response_model=PostOutput, tags=["records"], summary="검사 상태 및 기록 상세 조회")
    def detail(post_id: UUID, user: Annotated[str, Depends(current_user)]):
        return representation(visible_row(post_id.hex, user))

    @app.get("/posts/{post_id}/image", tags=["records"], summary="상태·소유권을 확인한 이미지 제공")
    def photo(post_id: UUID, user: Annotated[str, Depends(current_user)]):
        for _ in range(2):
            row = visible_row(post_id.hex, user)
            if row["status"] == "rejected":
                fail(404, "NOT_FOUND", "게시할 수 없는 사진이에요.")
            approved = row["status"] == "approved"
            try:
                content = store.path(post_id.hex, "jpg" if approved else "upload").read_bytes()
                return Response(content, media_type="image/jpeg" if approved else row["mime"])
            except FileNotFoundError:
                # A worker may finish and remove the original between the DB read and file open.
                continue
        fail(404, "NOT_FOUND", "사진이 더 이상 보관되어 있지 않아요.")

    @app.post("/posts/{post_id}/retry", status_code=202, response_model=PostOutput, tags=["records"], summary="검사 오류·시간 초과 재시도")
    def retry(post_id: UUID, user: Annotated[str, Depends(current_user)]):
        return representation(store.retry(post_id.hex, user))

    @app.get("/notifications", response_model=NotificationList, tags=["notifications"], summary="앱 알림·재접속 시 결과 확인")
    def notifications(user: Annotated[str, Depends(current_user)], unread_only: bool = True,
                      limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        with store.connect() as db:
            rows = db.execute("""SELECT id,post_id,kind,message,created_at,read_at FROM notifications
                WHERE owner=? AND (?=0 OR read_at IS NULL) ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""",
                              (user, unread_only, limit, offset)).fetchall()
        return {"items": [dict(r) for r in rows]}

    @app.post("/notifications/{notification_id}/read", tags=["notifications"])
    def read_notification(notification_id: UUID, user: Annotated[str, Depends(current_user)]):
        with store.connect() as db:
            changed = db.execute("UPDATE notifications SET read_at=coalesce(read_at,?) WHERE id=? AND owner=?",
                                 (time.time(), notification_id.hex, user)).rowcount
        if not changed:
            fail(404, "NOT_FOUND", "알림을 찾을 수 없어요.")
        return {"read": True}

    return app


app = create_app()
