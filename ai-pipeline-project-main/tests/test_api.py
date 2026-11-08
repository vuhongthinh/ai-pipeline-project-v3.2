"""API tests cho /upload, /task và /health: dò MIME theo nội dung, whitelist
ảnh, giới hạn kích thước, cleanup khi Redis lỗi, 404 task lạ và API key tuỳ
chọn. MinIO, Celery và Redis được giả lập nên không cần hạ tầng thật.

Chạy: python -m pytest tests/ -v
"""
import io
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import main  # noqa: E402


class FakeObject:
    def __init__(self, name):
        self.object_name = name


class FakeObjectStore:
    """MinIO giả: ghi nhận put/remove/list, có cờ bấm lỗi để mô phỏng sự cố."""

    def __init__(self):
        self.buckets = set()
        self.objects = {}
        self.fail_put = False
        self.fail_remove = False
        self.removed = []

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def put_object(self, bucket, object_name, data, length, content_type=None):
        if self.fail_put:
            raise ConnectionError("minio down (mô phỏng)")
        self.objects[object_name] = {
            "bucket": bucket,
            "data": data.read(),
            "content_type": content_type,
        }

    def remove_object(self, bucket, object_name):
        if self.fail_remove:
            raise ConnectionError("minio down khi dọn rác (mô phỏng)")
        self.removed.append(object_name)
        self.objects.pop(object_name, None)

    def list_objects(self, bucket, prefix=None, **kwargs):
        return iter([FakeObject(n) for n in sorted(self.objects)
                     if prefix and n.startswith(prefix)])


class FakeCelery:
    def __init__(self):
        self.sent = []
        self.fail_send = False

    def send_task(self, name, args=None, task_id=None, **kwargs):
        if self.fail_send:
            raise ConnectionError("redis down (mô phỏng)")
        self.sent.append({"name": name, "args": args, "task_id": task_id})
        return task_id


@pytest.fixture()
def env(monkeypatch):
    store = FakeObjectStore()
    celery = FakeCelery()
    monkeypatch.setattr(main, "get_minio_client", lambda: store)
    monkeypatch.setattr(main, "check_redis", lambda: True)
    monkeypatch.setattr(main.celery_app, "send_task", celery.send_task)
    monkeypatch.setattr(main, "MAX_FILE_SIZE", 1024 * 1024)
    monkeypatch.setattr(main, "MAX_IMAGE_SIZE", 10 * 1024)
    return {"store": store, "celery": celery}


@pytest.fixture()
def client(env):
    with TestClient(main.app) as test_client:
        yield test_client


def png_bytes(width=40, height=20):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 30, 90)).save(buf, format="PNG")
    return buf.getvalue()


class TestUploadHappyPaths:
    def test_png_declared_as_octet_stream_still_accepted(self, client, env):
        # Trình duyệt hay gửi application/octet-stream: dò theo nội dung phải cứu được.
        response = client.post(
            "/upload",
            files={"file": ("photo.png", png_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "accepted"
        assert body["mime_type"] == "image/png"
        assert body["bucket"] == "uploads"
        assert body["object_key"].endswith(".png")
        # Task được gửi qua send_task với bucket + object_key, không phải URL.
        assert env["celery"].sent and env["celery"].sent[0]["name"] == "tasks.process_file"
        assert env["celery"].sent[0]["args"] == [
            body["task_id"], "uploads", body["object_key"], "image/png",
        ]
        assert env["celery"].sent[0]["task_id"] == body["task_id"]

    def test_markdown_declared_as_octet_stream_accepted(self, client, env):
        response = client.post(
            "/upload",
            files={"file": ("note.md", "# Tiếng Việt\nNội dung".encode("utf-8"), "application/octet-stream")},
        )
        assert response.status_code == 202
        assert response.json()["mime_type"] == "text/markdown"

    def test_json_accepted(self, client, env):
        response = client.post(
            "/upload",
            files={"file": ("data.json", b'{"a": 1}', "application/json")},
        )
        assert response.status_code == 202
        assert response.json()["mime_type"] == "application/json"

    def test_mp3_declared_as_octet_stream_detected(self, client, env):
        # Magic bytes MP3 (ID3 header).
        mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 200
        response = client.post(
            "/upload",
            files={"file": ("clip.mp3", mp3, "application/octet-stream")},
        )
        assert response.status_code == 202
        assert response.json()["mime_type"] == "audio/mpeg"


class TestUploadRejections:
    def test_empty_file_400(self, client, env):
        response = client.post(
            "/upload",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert response.status_code == 400

    def test_oversized_file_413(self, client, env):
        response = client.post(
            "/upload",
            files={"file": ("big.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
        )
        assert response.status_code == 413

    def test_oversized_image_413(self, client, env):
        response = client.post(
            "/upload",
            files={"file": ("big.png", png_bytes() + b"\x00" * (11 * 1024), "image/png")},
        )
        assert response.status_code == 413

    def test_svg_rejected_415(self, client, env):
        # Whitelist ảnh chỉ còn jpeg/png/webp: SVG phải chặn trước khi tới VLM.
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'
        response = client.post(
            "/upload",
            files={"file": ("evil.svg", svg, "image/svg+xml")},
        )
        assert response.status_code == 415
        assert env["celery"].sent == []

    def test_tiff_rejected_415_even_with_spoofed_header(self, client, env):
        # Magic bytes TIFF thật + header client giả image/png: nội dung phải thắng.
        tiff = b"II*\x00" + b"\x00" * 200
        response = client.post(
            "/upload",
            files={"file": ("img.tiff", tiff, "image/png")},
        )
        assert response.status_code == 415

    def test_exe_disguised_as_text_rejected(self, client, env):
        exe = b"MZ\x90\x00" + b"\x00" * 200
        response = client.post(
            "/upload",
            files={"file": ("payload.txt", exe, "text/plain")},
        )
        assert response.status_code == 415

    def test_video_rejected_415(self, client, env):
        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200
        response = client.post(
            "/upload",
            files={"file": ("video.mp4", mp4, "video/mp4")},
        )
        assert response.status_code == 415


class TestOutageHandling:
    def test_redis_down_rolls_back_object_and_returns_503(self, client, env):
        # Upload thành công nhưng Redis lỗi -> không được để lại file rác.
        env["celery"].fail_send = True
        response = client.post(
            "/upload",
            files={"file": ("photo.png", png_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 503
        assert "rolled back" in response.json()["detail"]
        assert env["store"].objects == {}  # object đã bị dọn
        assert len(env["store"].removed) == 1

    def test_minio_down_returns_503_and_never_queues_task(self, client, env):
        env["store"].fail_put = True
        response = client.post(
            "/upload",
            files={"file": ("photo.png", png_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 503
        assert "Object store unavailable" in response.json()["detail"]
        assert env["celery"].sent == []

    def test_health_reports_redis_outage(self, client, env, monkeypatch):
        def broken_redis():
            raise ConnectionError("redis down (mô phỏng)")

        monkeypatch.setattr(main, "check_redis", broken_redis)
        response = client.get("/health")
        assert response.status_code == 503
        assert "redis" in response.json()["detail"]

    def test_health_reports_minio_outage(self, client, env, monkeypatch):
        def broken_minio():
            raise ConnectionError("minio down (mô phỏng)")

        monkeypatch.setattr(main, "get_minio_client", broken_minio)
        response = client.get("/health")
        assert response.status_code == 503
        assert "minio" in response.json()["detail"]


class TestTaskStatus:
    @pytest.fixture()
    def statuses(self, monkeypatch):
        """Thay AsyncResult thật (cần Redis) bằng bản giả lập."""
        captured = {}

        class FakeAsyncResult:
            def __init__(self, task_id, app=None):
                self.task_id = task_id

            @property
            def state(self):
                return captured.get(self.task_id, "PENDING")

            @property
            def result(self):
                return {"short_label": "nhãn", "labels": []}

        monkeypatch.setattr("celery.result.AsyncResult", FakeAsyncResult)
        return captured

    def test_unknown_task_returns_404(self, client, env, statuses):
        # PENDING với id chưa từng upload -> 404 chứ không phải "queued" mơ hồ.
        response = client.get(f"/task/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["detail"] == "Unknown task id"

    def test_pending_with_object_is_queued(self, client, env, statuses):
        task_id = str(uuid.uuid4())
        env["store"].objects[f"{task_id}.txt"] = {"bucket": "uploads", "data": b"", "content_type": "text/plain"}
        response = client.get(f"/task/{task_id}")
        assert response.status_code == 200
        assert response.json() == {"status": "queued"}

    def test_finished_task(self, client, env, statuses):
        task_id = str(uuid.uuid4())
        statuses[task_id] = "SUCCESS"
        response = client.get(f"/task/{task_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "finished"
        assert body["result"]["short_label"] == "nhãn"

    def test_failed_task(self, client, env, statuses):
        task_id = str(uuid.uuid4())
        statuses[task_id] = "FAILURE"
        response = client.get(f"/task/{task_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "failed"


class TestApiKey:
    def test_no_key_configured_open_access(self, client, env, monkeypatch):
        monkeypatch.delenv("API_KEY", raising=False)
        response = client.post(
            "/upload",
            files={"file": ("photo.png", png_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 202

    def test_wrong_key_rejected(self, client, env, monkeypatch):
        monkeypatch.setenv("API_KEY", "topsecret")
        response = client.post(
            "/upload",
            files={"file": ("photo.png", png_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 401

    def test_correct_key_accepted(self, client, env, monkeypatch):
        monkeypatch.setenv("API_KEY", "topsecret")
        response = client.post(
            "/upload",
            files={"file": ("photo.png", png_bytes(), "application/octet-stream")},
            headers={"X-API-Key": "topsecret"},
        )
        assert response.status_code == 202


class TestMisc:
    def test_health_ok(self, client, env):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "minio": True, "redis": True}

    def test_serve_ui_contains_dynamic_result_hook(self, client, env):
        response = client.get("/")
        assert response.status_code == 200
        # Regression cho bug UI: phần tử động phải được đánh dấu bằng attr riêng.
        assert "data-dynamic-result" in response.text
        assert "MAX_POLLS" in response.text
        # Polling phải dừng khi gặp 404 (task không tồn tại).
        assert "response.status === 404" in response.text

    def test_file_object_saved_with_detected_mime(self, client, env):
        client.post(
            "/upload",
            files={"file": ("photo.png", png_bytes(), "application/octet-stream")},
        )
        saved = list(env["store"].objects.values())
        assert len(saved) == 1
        assert saved[0]["content_type"] == "image/png"
