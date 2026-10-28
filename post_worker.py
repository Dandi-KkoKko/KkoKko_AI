"""Run alongside the API: python post_worker.py (same FACE_DATA_DIR).

One durable SQLite queue, one model process. A killed/timed-out detector is replaced
before the next job. Unpublished originals expire after 24 hours.
"""
from contextlib import contextmanager
import logging
import multiprocessing as mp
import os
import signal
import threading
import time

from post_store import DATA_DIR, Store

log = logging.getLogger(__name__)


class InvalidPhoto(Exception):
    pass


def detector_process(pipe):
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    try:
        import cv2
        import face_check
        cv2.setNumThreads(1)
        assert face_check.MAX_SIDE == 1920 and face_check.STORE_SIDE == 1080
        assert face_check.SCORE_THRESHOLD == 0.5
        pipe.send(("ready", None))
        while (path := pipe.recv()) is not None:
            try:
                from pathlib import Path
                jpeg, faces = face_check.check(Path(path).read_bytes())
                pipe.send(("ok", (jpeg, bool(faces))))
            except face_check.InvalidImage:
                pipe.send(("invalid", None))
            except Exception:
                log.exception("Detector failed")
                pipe.send(("error", None))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        pipe.close()


class Detector:
    def __init__(self, timeout=45, startup_timeout=60, target=detector_process):
        self.timeout, self.startup_timeout, self.target = timeout, startup_timeout, target
        self.process = self.pipe = None

    def stop(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
            self.process.close()
            self.pipe.close()
        self.process = self.pipe = None

    def receive(self, timeout):
        if not self.pipe.poll(timeout):
            raise TimeoutError("Detection deadline exceeded")
        return self.pipe.recv()

    def __call__(self, path):
        try:
            if self.process is None:
                ctx = mp.get_context("spawn")
                self.pipe, child = ctx.Pipe()
                self.process = ctx.Process(target=self.target, args=(child,), daemon=True)
                self.process.start()
                child.close()
                if self.receive(self.startup_timeout)[0] != "ready":
                    raise RuntimeError("Model unavailable")
            self.pipe.send(str(path))
            status, result = self.receive(self.timeout)
            if status == "invalid":
                raise InvalidPhoto()
            if status != "ok":
                raise RuntimeError("Detection failed")
            return result
        except InvalidPhoto:
            raise
        except Exception:
            self.stop()
            raise


@contextmanager
def single_worker(store):
    with (store.root / "worker.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("A worker already owns this data directory") from error
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_once(store, detector):
    row = store.claim()
    if not row:
        return False
    post_id = row["id"]
    temporary = store.path(post_id, "part")
    try:
        jpeg, has_faces = detector(store.path(post_id))
        if has_faces:
            store.finish(post_id, "rejected", "FACE_DETECTED")
        else:
            if not isinstance(jpeg, bytes) or not jpeg:
                raise RuntimeError("Detector returned no image")
            with temporary.open("wb") as output:
                output.write(jpeg)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(store.path(post_id, "jpg"))
            store.finish(post_id, "approved")
    except InvalidPhoto:
        store.finish(post_id, "rejected", "INVALID_IMAGE")
    except TimeoutError:
        store.finish(post_id, "failed", "DETECTION_TIMEOUT")
    except Exception:
        log.exception("Post %s could not be checked", post_id)
        store.finish(post_id, "failed", "DETECTION_UNAVAILABLE")
    finally:
        temporary.unlink(missing_ok=True)
    if store.get(post_id)["status"] in ("approved", "rejected"):
        store.path(post_id).unlink(missing_ok=True)
    return True


def work(root=DATA_DIR, stop_event=None):
    logging.basicConfig(level=logging.INFO)
    store = Store(root)
    stop_event = stop_event or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    detector = Detector()
    with single_worker(store):
        store.recover()
        next_cleanup = 0
        try:
            while not stop_event.is_set():
                if time.monotonic() >= next_cleanup:
                    store.cleanup()
                    next_cleanup = time.monotonic() + 60
                if not run_once(store, detector):
                    stop_event.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            detector.stop()


if __name__ == "__main__":
    work()
