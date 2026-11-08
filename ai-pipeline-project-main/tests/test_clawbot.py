"""Regression test cho lỗi "luôn báo API từ chối dù API trả 202"."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mini_clawbot  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    @property
    def ok(self):
        return self.status_code < 400


def test_accepts_202(monkeypatch, capsys):
    captured = {}

    def fake_post(url, files=None, headers=None, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["headers"] = headers
        return FakeResponse(202, {"status": "accepted", "task_id": "abc-123"})

    monkeypatch.setattr(mini_clawbot.requests, "post", fake_post)
    monkeypatch.setattr(mini_clawbot, "API_KEY", "")  # chưa bật xác thực
    mini_clawbot.crawl_and_upload()
    out = capsys.readouterr().out
    assert "abc-123" in out
    assert "❌" not in out
    assert captured["timeout"] is not None
    assert captured["headers"] is None


def test_sends_api_key_header_when_configured(monkeypatch, capsys):
    def fake_post(url, files=None, headers=None, timeout=None):
        assert headers == {"X-API-Key": "topsecret"}
        return FakeResponse(202, {"status": "accepted", "task_id": "key-1"})

    monkeypatch.setattr(mini_clawbot.requests, "post", fake_post)
    monkeypatch.setattr(mini_clawbot, "API_KEY", "topsecret")
    mini_clawbot.crawl_and_upload()
    out = capsys.readouterr().out
    assert "key-1" in out


def test_rejects_4xx(monkeypatch, capsys):
    def fake_post(url, files=None, headers=None, timeout=None):
        return FakeResponse(415, {"detail": "Unsupported content type: application/zip"})

    monkeypatch.setattr(mini_clawbot.requests, "post", fake_post)
    mini_clawbot.crawl_and_upload()
    out = capsys.readouterr().out
    assert "❌" in out
    assert "Unsupported" in out
