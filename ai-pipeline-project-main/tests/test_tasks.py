"""Unit tests cho các hàm parse/trợ giúp trong tasks.py.

Chạy: python -m pytest tests/ -v
Không cần Postgres/Redis/MinIO: pool DB giờ được tạo lười nên import tasks
đơn thuần không mở kết nối nào.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import tasks  # noqa: E402
from tasks import (  # noqa: E402
    PermanentTaskError,
    as_string_list,
    parse_json_object,
    shrink_image,
)


class TestParseJsonObject:
    def test_valid_json(self):
        assert parse_json_object('{"a": 1}', {}) == {"a": 1}

    def test_json_wrapped_in_prose(self):
        raw = 'Kết quả đây: {"labels": ["x"], "description": "y"} mong bạn thông cảm.'
        assert parse_json_object(raw, {}) == {"labels": ["x"], "description": "y"}

    def test_json_array_is_not_object(self):
        assert parse_json_object("[1, 2]", {"fallback": True}) == {"fallback": True}

    def test_garbage_returns_fallback(self):
        assert parse_json_object("không có json nào", {"fallback": True}) == {"fallback": True}

    def test_none_returns_fallback(self):
        assert parse_json_object(None, {"fallback": True}) == {"fallback": True}

    def test_broken_json_returns_fallback(self):
        assert parse_json_object('{"a": [1,', {"fallback": True}) == {"fallback": True}


class TestAsStringList:
    def test_non_list_returns_empty(self):
        assert as_string_list("abc") == []
        assert as_string_list(None) == []
        assert as_string_list({"a": 1}) == []

    def test_strips_and_filters_empty(self):
        # Giữ nguyên hành vi gốc: chỉ lọc chuỗi rỗng sau strip (None -> "None" được giữ).
        assert as_string_list([" a ", "", 3]) == ["a", "3"]

    def test_caps_at_20_items(self):
        assert len(as_string_list(list(range(50)))) == 20


class FakePool:
    def __init__(self, *args, **kwargs):
        pass

    def closeall(self):
        pass


class TestImportSafety:
    def test_import_tasks_does_not_open_db_pool(self):
        # Regression cho lỗi "API không khởi động được nếu Postgres chưa chạy":
        # pool phải được tạo lười, không phải lúc import.
        assert tasks._db_pool is None

    def test_get_db_pool_is_lazy_and_singleton(self, monkeypatch):
        created = []

        def fake_pool_cls(*args, **kwargs):
            created.append(1)
            return FakePool()

        monkeypatch.setattr(tasks.pool, "SimpleConnectionPool", fake_pool_cls)
        tasks._db_pool = None
        assert tasks._db_pool is None  # chưa ai dùng DB -> chưa tạo pool
        first = tasks.get_db_pool()
        assert first is tasks.get_db_pool()  # singleton
        assert len(created) == 1  # chỉ dựng pool đúng một lần
        tasks._db_pool = None


class TestShrinkImage:
    def _png(self, width, height):
        buf = io.BytesIO()
        from PIL import Image

        Image.new("RGB", (width, height), color=(10, 120, 200)).save(buf, format="PNG")
        return buf.getvalue()

    def test_small_image_kept_as_is(self):
        data = self._png(64, 32)
        assert shrink_image(data) == data

    def test_large_image_downscaled(self):
        from PIL import Image

        data = self._png(2000, 1000)
        out = shrink_image(data)
        img = Image.open(io.BytesIO(out))
        assert max(img.size) <= 1024
        # Giữ nguyên định dạng PNG sau resize (không bị đổi thành JPEG).
        assert img.format == "PNG"

    def test_broken_image_raises_unidentified(self):
        from PIL import UnidentifiedImageError

        with pytest.raises(UnidentifiedImageError):
            shrink_image(b"day khong phai anh")


class TestErrorClassification:
    def test_permanent_http_status_set(self):
        assert tasks.PERMANENT_HTTP_STATUS == {400, 404, 413, 415, 422}

    def test_permanent_error_type_exists(self):
        assert issubclass(PermanentTaskError, Exception)


class TestAskOllamaPayload:
    def test_chat_payload_has_num_ctx_and_system_message(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": "{}"}}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

        monkeypatch.setattr(tasks.requests, "post", fake_post)
        out = tasks.ask_ollama("m", "SYSTEM", "USER", ["img"])
        assert captured["url"].endswith("/api/chat")
        payload = captured["payload"]
        assert payload["options"]["num_ctx"] == tasks.OLLAMA_NUM_CTX
        assert payload["keep_alive"]
        assert payload["messages"][0] == {"role": "system", "content": "SYSTEM"}
        assert payload["messages"][1]["content"] == "USER"
        assert payload["messages"][1]["images"] == ["img"]
        assert out == "{}"


class TestProcessFileRouting:
    def test_unsupported_mime_fails_fast(self):
        # Unsupported MIME phải fail ngay, không retry.
        with pytest.raises(ValueError):
            tasks.process_file.run("tid", "bucket", "key.bin", "application/x-unknown")

    def test_task_registered_on_shared_app(self):
        from celery_app import celery_app

        assert "tasks.process_file" in celery_app.tasks


@pytest.fixture(autouse=True)
def _restore_pool():
    yield
    if tasks._db_pool is not None:
        tasks._db_pool.closeall()
        tasks._db_pool = None
