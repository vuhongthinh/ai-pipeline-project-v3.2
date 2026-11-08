"""Tests cho dashboard: presigned URL phải được KÝ bằng endpoint công khai
(MINIO_PUBLIC_URL) vì chữ ký SigV4 tính cả header Host — trình duyệt Windows
không phân giải được hostname nội bộ "minio".

Chạy: python -m pytest tests/ -v
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402  (import an toàn: toàn bộ luồng Streamlit nằm trong main())


class TestPresignPublicUrl:
    def test_signed_with_public_endpoint(self, monkeypatch):
        monkeypatch.setenv("MINIO_PUBLIC_URL", "mylaptop.example.com:9000")
        monkeypatch.setenv("MINIO_ROOT_USER", "user1")
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "pass1")
        monkeypatch.delenv("MINIO_SECURE", raising=False)

        client = dashboard.get_presign_client()
        url = client.presigned_get_object("uploads", "abc/x.png", expires=timedelta(hours=1))

        # URL mở được từ trình duyệt: host công khai, có chữ ký đầy đủ.
        assert url.startswith("http://mylaptop.example.com:9000/uploads/abc/x.png?")
        assert "X-Amz-Signature" in url
        assert "X-Amz-Credential=user1" in url

    def test_default_public_endpoint_is_localhost(self, monkeypatch):
        monkeypatch.delenv("MINIO_PUBLIC_URL", raising=False)
        client = dashboard.get_presign_client()
        url = client.presigned_get_object("uploads", "k.bin", expires=timedelta(hours=1))
        assert url.startswith("http://localhost:9000/uploads/k.bin?")

    def test_never_signs_with_internal_hostname(self, monkeypatch):
        # Ngay cả khi MINIO_URL (nội bộ) là minio:9000, chữ ký vẫn dùng public URL.
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_PUBLIC_URL", "localhost:9000")
        url = dashboard.get_presign_client().presigned_get_object(
            "uploads", "k.bin", expires=timedelta(hours=1)
        )
        assert url.startswith("http://localhost:9000/")
        assert not url.startswith("http://minio:9000/")
