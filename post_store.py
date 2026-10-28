import json
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


DATA_DIR = Path(os.environ.get("FACE_DATA_DIR", Path(__file__).parent / "private_data"))
MESSAGES = {
    "pending": "사진 확인을 기다리고 있어요. 나에게만 보여요.",
    "processing": "사진을 확인하고 있어요. 나에게만 보여요.",
    "scheduled": "얼굴이 감지되지 않았어요. 공개 예정 시간까지 나에게만 보여요.",
    "published": "얼굴이 감지되지 않아 기록이 공개됐어요.",
    "FACE_DETECTED": "얼굴이 감지되어 업로드할 수 없는 사진입니다. 다른 사진을 선택해주세요.",
    "INVALID_IMAGE": "사진 파일을 읽을 수 없어요. 다른 사진을 선택해주세요.",
    "DETECTION_UNAVAILABLE": "사진을 확인하지 못했어요. 다시 시도해주세요.",
    "DETECTION_TIMEOUT": "사진 확인 시간이 초과됐어요. 다시 시도해주세요.",
    "EXPIRED": "검사 대기 기간이 지나 사진을 삭제했어요. 다시 올려주세요.",
}


class StoreError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class Store:
    def __init__(self, root=DATA_DIR):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "posts.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, metadata TEXT NOT NULL, mime TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, publish_at REAL NOT NULL,
                    UNIQUE(owner, request_id)
                );
                CREATE INDEX IF NOT EXISTS posts_queue ON posts(status, created_at);
                CREATE INDEX IF NOT EXISTS posts_owner ON posts(owner, created_at);
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, post_id TEXT NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL,
                    read_at REAL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, post_id, suffix="upload"):
        return self.root / f"{post_id}.{suffix}"

    def get(self, post_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            return dict(row) if row else None

    def submit(self, post_id, owner, request_id, fingerprint, metadata, mime, delay):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM posts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise StoreError("IDEMPOTENCY_CONFLICT")
                return dict(existing)
            self.check_capacity(db, owner)
            db.execute("INSERT INTO posts VALUES (?,?,?,?,?,?, 'pending',NULL,?,?,?)",
                       (post_id, owner, request_id, fingerprint, json.dumps(metadata, ensure_ascii=False), mime,
                        now, now, now + delay))
        return self.get(post_id)

    @staticmethod
    def check_capacity(db, owner):
        counts = db.execute("""SELECT count(*), coalesce(sum(owner=?),0) FROM posts
            WHERE status IN ('pending','processing','failed')""", (owner,)).fetchone()
        if counts[0] >= 50 or counts[1] >= 3:
            raise StoreError("QUEUE_FULL", 429)

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM posts WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE posts SET status='processing',updated_at=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def recover(self):
        # Only the process holding worker.lock may recover an interrupted job.
        with self.connect() as db:
            db.execute("UPDATE posts SET status='pending' WHERE status='processing'")

    def finish(self, post_id, status, reason=None):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM posts WHERE id=? AND status='processing'", (post_id,)).fetchone()
            if not row:
                raise StoreError("INVALID_TRANSITION")
            db.execute("UPDATE posts SET status=?,reason=?,updated_at=? WHERE id=?", (status, reason, now, post_id))
            kind = reason or ("scheduled" if now < row["publish_at"] else "published")
            db.execute("INSERT INTO notifications VALUES (?,?,?,?,?,?,NULL)",
                       (uuid4().hex, row["owner"], post_id, kind, MESSAGES[kind], now))

    def retry(self, post_id, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM posts WHERE id=? AND owner=?", (post_id, owner)).fetchone()
            if not row:
                raise StoreError("NOT_FOUND", 404)
            if row["status"] in ("pending", "processing"):
                return dict(row)
            if row["status"] != "failed" or not self.path(post_id).exists():
                raise StoreError("REUPLOAD_REQUIRED")
            db.execute("UPDATE posts SET status='pending',reason=NULL,updated_at=? WHERE id=?", (time.time(), post_id))
            db.execute("UPDATE notifications SET read_at=coalesce(read_at,?) WHERE post_id=?", (time.time(), post_id))
        return self.get(post_id)

    def cleanup(self):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT * FROM posts WHERE status IN ('pending','failed') AND created_at<?", (now - 86400,)).fetchall()
            for row in expired:
                db.execute("UPDATE posts SET status='rejected',reason='EXPIRED',updated_at=? WHERE id=?", (now, row["id"]))
                db.execute("INSERT INTO notifications VALUES (?,?,?,?,?,?,NULL)",
                           (uuid4().hex, row["owner"], row["id"], "EXPIRED", MESSAGES["EXPIRED"], now))
            rows = db.execute("SELECT id,status FROM posts").fetchall()
        states = {r["id"]: r["status"] for r in rows}
        for path in self.root.iterdir():
            if path.suffix not in (".upload", ".jpg", ".part"):
                continue
            status = states.get(path.stem)
            if (path.suffix == ".upload" and status in ("approved", "rejected")) or (path.suffix in (".jpg", ".part") and status == "rejected"):
                path.unlink(missing_ok=True)
            elif status is None and path.stat().st_mtime < now - 3600:
                path.unlink(missing_ok=True)


def public(row, now=None):
    return row["status"] == "approved" and row["publish_at"] <= (time.time() if now is None else now)


def representation(row):
    status = row["status"]
    if status == "approved":
        status = "published" if public(row) else "scheduled"
    return {
        "id": row["id"], **json.loads(row["metadata"]), "status": status,
        "visibility": "public" if status == "published" else "owner_only",
        "message": MESSAGES[row["reason"] or status], "reason_code": row["reason"],
        "created_at": row["created_at"], "publish_at": row["publish_at"],
        "image_url": None if status == "rejected" else f"/posts/{row['id']}/image",
        "can_retry": status == "failed",
        "poll_after_seconds": 3 if status in ("pending", "processing") else (60 if status == "scheduled" else None),
    }
