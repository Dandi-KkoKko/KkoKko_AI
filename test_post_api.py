import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from post_api import create_app, MAX_BODY_BYTES
from post_store import Store
from post_worker import Detector, InvalidPhoto, run_once, single_worker, work

SAMPLES = Path(__file__).parent / "tests"
BUILDING = (SAMPLES / "building.jpg").read_bytes()
ASTRONAUT = (SAMPLES / "astronaut.png").read_bytes()
ALICE = {"Authorization": "Bearer alice-test"}
BOB = {"Authorization": "Bearer bob-test"}


def hung_detector(pipe):
    pipe.send(("ready", None))
    pipe.recv()
    time.sleep(120)


def crashed_detector(pipe):
    pipe.send(("ready", None))
    pipe.recv()
    os._exit(1)


class PostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, {"alice-test": "alice", "bob-test": "bob"}, public_delay=0)
        self.client = TestClient(self.app)
        self.store = self.app.state.store

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def upload(self, photo=BUILDING, metadata=None, headers=ALICE):
        fields = {"client_request_id": str(uuid4()), "title": "골목", "caption": "조용한 오후",
                  "latitude": 35.87, "longitude": 128.60, "hashtags": ["산책"]}
        if metadata:
            fields.update(metadata)
        return self.client.post("/posts", headers=headers, data={"metadata": json.dumps(fields)},
                                files={"image": ("사진.jpg", photo, "image/jpeg")})

    def test_pending_owner_only_then_public(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.text)
        post = response.json()
        self.assertEqual(post["status"], "pending")
        self.assertEqual(self.client.get("/posts").json()["items"], [])
        self.assertEqual(self.client.get("/posts/mine", headers=BOB).json()["total"], 0)
        self.assertEqual(self.client.get(f"/posts/{post['id']}", headers=BOB).status_code, 404)
        self.assertEqual(self.client.get(post["image_url"], headers=BOB).status_code, 404)
        self.assertEqual(self.client.get(post["image_url"], headers=ALICE).content, BUILDING)
        self.assertNotIn("owner", post)
        run_once(self.store, lambda path: (ASTRONAUT, False))
        self.assertEqual(self.client.get("/posts").json()["items"][0]["status"], "published")
        self.assertEqual(self.client.get(post["image_url"], headers=BOB).content, ASTRONAUT)
        self.assertFalse(self.store.path(post["id"]).exists())
        self.assertIn("no-store", self.client.get(post["image_url"], headers=BOB).headers["cache-control"])

    def test_delay_is_enforced_for_detail_image_and_list(self):
        self.client.close()
        self.app = create_app(self.temp.name, {"alice-test": "alice", "bob-test": "bob"}, public_delay=7200)
        self.client = TestClient(self.app)
        post = self.upload().json()
        run_once(self.store, lambda path: (BUILDING, False))
        self.assertEqual(self.client.get(f"/posts/{post['id']}", headers=ALICE).json()["status"], "scheduled")
        self.assertEqual(self.client.get("/posts").json()["items"], [])
        self.assertEqual(self.client.get(post["image_url"], headers=BOB).status_code, 404)
        with self.store.connect() as db:
            db.execute("UPDATE posts SET publish_at=? WHERE id=?", (time.time() - 1, post["id"]))
        self.assertEqual(self.client.get(post["image_url"], headers=BOB).status_code, 200)

    def test_rejection_deletes_photo_and_persists_owner_notification(self):
        post = self.upload().json()
        run_once(self.store, lambda path: (BUILDING, True))
        detail = self.client.get(f"/posts/{post['id']}", headers=ALICE).json()
        self.assertEqual(detail["reason_code"], "FACE_DETECTED")
        self.assertIsNone(detail["image_url"])
        self.assertFalse(self.store.path(post["id"]).exists())
        self.assertFalse(self.store.path(post["id"], "jpg").exists())
        self.assertEqual(self.client.get(post["image_url"], headers=ALICE).status_code, 404)
        self.assertEqual(self.client.get("/notifications", headers=BOB).json()["items"], [])
        notification = self.client.get("/notifications", headers=ALICE).json()["items"][0]
        self.assertEqual(self.client.post(f"/notifications/{notification['id']}/read", headers=BOB).status_code, 404)
        self.client.post(f"/notifications/{notification['id']}/read", headers=ALICE)
        self.assertEqual(self.client.get("/notifications", headers=ALICE).json()["items"], [])
        self.assertEqual(self.client.post(f"/posts/{post['id']}/retry", headers=ALICE).status_code, 409)

    def test_timeout_remains_private_and_retry_works(self):
        post = self.upload().json()
        def timeout(path):
            raise TimeoutError()
        run_once(self.store, timeout)
        self.assertEqual(self.store.get(post["id"])["status"], "failed")
        self.assertTrue(self.store.path(post["id"]).exists())
        self.assertEqual(self.client.get("/posts").json()["items"], [])
        self.assertEqual(self.client.post(f"/posts/{post['id']}/retry", headers=BOB).status_code, 404)
        self.assertEqual(self.client.post(f"/posts/{post['id']}/retry", headers=ALICE).json()["status"], "pending")
        run_once(self.store, lambda path: (BUILDING, False))
        self.assertEqual(self.store.get(post["id"])["status"], "approved")

    def test_model_error_and_bad_decode_never_publish(self):
        for error, expected in [(RuntimeError("model failed"), "failed"), (InvalidPhoto(), "rejected")]:
            post = self.upload().json()
            def broken(path):
                raise error
            with self.assertLogs("post_worker", level="ERROR") if expected == "failed" else single_worker(self.store):
                run_once(self.store, broken)
            self.assertEqual(self.store.get(post["id"])["status"], expected)
            self.assertEqual(self.client.get("/posts").json()["items"], [])

    def test_idempotency_and_conflicting_reuse(self):
        metadata = {"client_request_id": str(uuid4())}
        first = self.upload(metadata=metadata).json()
        self.assertEqual(self.upload(metadata=metadata).json()["id"], first["id"])
        self.assertEqual(self.upload(photo=ASTRONAUT, metadata=metadata).status_code, 409)
        self.assertEqual(len(list(self.store.root.glob("*.upload"))), 1)

    def test_auth_validation_and_queue_limits(self):
        self.assertEqual(self.upload(headers={}).status_code, 401)
        self.assertEqual(self.upload(metadata={"owner": "bob"}).status_code, 422)
        self.assertEqual(self.upload(metadata={"approved": True}).status_code, 422)
        self.assertEqual(self.upload(metadata={"title": "a" * 11}).status_code, 422)
        self.assertEqual(self.upload(metadata={"caption": "a" * 41}).status_code, 422)
        self.assertEqual(self.upload(metadata={"hashtags": ["a", "b", "c", "d"]}).status_code, 422)
        self.assertEqual(self.upload(metadata={"latitude": 999}).status_code, 422)
        self.assertEqual(self.upload(photo=b"not an image").status_code, 400)
        for _ in range(3):
            self.assertEqual(self.upload().status_code, 202)
        self.assertEqual(self.upload().status_code, 429)
        self.assertEqual(len(list(self.store.root.glob("*.upload"))), 3)

    def test_body_limit_without_content_length(self):
        boundary = "limit-test"
        def chunks():
            yield f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="big.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
            for _ in range(MAX_BODY_BYTES // (64 * 1024) + 2):
                yield b"x" * (64 * 1024)
            yield f"\r\n--{boundary}--\r\n".encode()
        response = self.client.post("/posts", headers={**ALICE, "Content-Type": f"multipart/form-data; boundary={boundary}"}, content=chunks())
        self.assertEqual(response.status_code, 413, response.text)

    def test_restart_and_single_worker_lock(self):
        post = self.upload().json()
        self.store.claim()
        restarted = Store(self.temp.name)
        with single_worker(restarted):
            with self.assertRaises(RuntimeError):
                with single_worker(restarted):
                    pass
            restarted.recover()
            run_once(restarted, lambda path: (BUILDING, False))
        self.assertEqual(restarted.get(post["id"])["status"], "approved")

    def test_expiration_deletes_unchecked_file(self):
        post = self.upload().json()
        with self.store.connect() as db:
            db.execute("UPDATE posts SET created_at=? WHERE id=?", (time.time() - 86401, post["id"]))
        self.store.cleanup()
        self.assertEqual(self.store.get(post["id"])["reason"], "EXPIRED")
        self.assertFalse(self.store.path(post["id"]).exists())

    def test_process_timeout_is_actually_stopped(self):
        detector = Detector(timeout=0.1, startup_timeout=30, target=hung_detector)
        try:
            with self.assertRaises(TimeoutError):
                detector("unused")
            self.assertIsNone(detector.process)
        finally:
            detector.stop()

    def test_process_crash_is_not_approval(self):
        detector = Detector(startup_timeout=30, target=crashed_detector)
        try:
            with self.assertRaises((EOFError, BrokenPipeError, OSError)):
                detector("unused")
            self.assertIsNone(detector.process)
        finally:
            detector.stop()

    def test_real_scrfd_from_upload_to_result(self):
        detector = Detector()
        try:
            for image, expected in [(BUILDING, "approved"), (ASTRONAUT, "rejected")]:
                post = self.upload(photo=image).json()
                run_once(self.store, detector)
                self.assertEqual(self.store.get(post["id"])["status"], expected)
        finally:
            detector.stop()

    def test_worker_loop_restarts_durable_job(self):
        post = self.upload().json()
        self.store.claim()
        ctx = mp.get_context("spawn")
        stopped = ctx.Event()
        worker = ctx.Process(target=work, args=(self.temp.name, stopped))
        worker.start()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and self.store.get(post["id"])["status"] != "approved":
                time.sleep(0.1)
            self.assertEqual(self.store.get(post["id"])["status"], "approved")
            self.assertEqual(self.client.get("/posts").json()["items"][0]["id"], post["id"])
        finally:
            stopped.set()
            worker.join(10)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            self.assertEqual(worker.exitcode, 0)
            worker.close()


if __name__ == "__main__":
    unittest.main()
