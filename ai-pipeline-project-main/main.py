import logging
import mimetypes
import os
import time
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

import filetype
from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader
from minio import Minio

# Only the Celery app - no tasks.py import, so the API never pulls in
# psycopg2/worker logic and can start before Postgres is up.
from celery_app import celery_app

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_bucket()
    yield


app = FastAPI(title="Multi-modal Ingestion API", version="1.2.0", lifespan=lifespan)

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))
MAX_IMAGE_SIZE = int(os.getenv("MAX_IMAGE_SIZE", str(10 * 1024 * 1024)))

# Client-declared MIME headers are untrusted; images are whitelisted by exact
# type so SVG/HEIC/TIFF never reach the vision model.
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXACT = {"application/json"}

# Tuỳ chọn: đặt API_KEY trong .env để bật xác thực cho /upload (header X-API-Key).
# Để trống khi chạy demo giao diện web (fetch cùng nguồn gốc không gửi header).
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_minio_client():
    return Minio(
        os.getenv("MINIO_URL", "minio:9000"),
        access_key=os.getenv("MINIO_ROOT_USER", "minioadmin"),
        secret_key=os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"),
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )


def check_redis():
    import redis  # đi kèm celery[redis]

    client = redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"),
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    return bool(client.ping())


def ensure_bucket():
    bucket = os.getenv("MINIO_BUCKET", "uploads")
    last_error = None
    for attempt in range(12):
        try:
            client = get_minio_client()
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(min(2 + attempt, 10))
    raise RuntimeError(f"MinIO did not become ready: {last_error}")


def require_api_key(x_api_key: str | None = Security(_api_key_header)):
    expected = os.getenv("API_KEY", "").strip()
    if not expected:
        return  # chưa bật xác thực (chế độ dev/demo)
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def detect_mime(content: bytes, filename: str, declared: str) -> str:
    """Best-effort MIME detection with content as the source of truth.

    Browsers sometimes send application/octet-stream for valid .md/.m4a
    files, and client headers can be spoofed, so: (1) sniff magic bytes with
    filetype, (2) fall back to the file extension, (3) fall back to the
    declared header last.
    """
    guess = filetype.guess(content)
    if guess:
        return guess.mime
    extension_type = mimetypes.guess_type(filename or "")[0]
    if extension_type:
        return extension_type.lower()
    return (declared or "application/octet-stream").lower()


def check_mime_allowed(mime_type: str) -> bool:
    if mime_type in ALLOWED_IMAGE_TYPES:
        return True
    return mime_type.startswith("audio/") or mime_type.startswith("text/") or mime_type in ALLOWED_EXACT


def task_object_exists(task_id: str) -> bool:
    """Task id lạ (chưa từng upload) không để lại dấu vết nào trong MinIO.

    object_key luôn bắt đầu bằng task_id, nên một lần list_objects với prefix
    là đủ phân biệt "queued thật" với "id không tồn tại" mà không cần bảng
    trạng thái trong DB.
    """
    client = get_minio_client()
    objects = client.list_objects(os.getenv("MINIO_BUCKET", "uploads"), prefix=task_id)
    return next(iter(objects), None) is not None


@app.middleware("http")
async def reject_oversize_early(request, call_next):
    """Chặn sớm request /upload vượt hạn mức ngay từ tầng HTTP, trước khi
    tốn băng thông/RAM đọc toàn bộ body."""
    if request.method == "POST" and request.url.path == "/upload":
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                # +1MB đủ chỗ cho phần ghi đề multipart.
                if int(content_length) > MAX_FILE_SIZE + 1024 * 1024:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Request exceeds {MAX_FILE_SIZE + 1024 * 1024} bytes"},
                    )
            except ValueError:
                pass
    return await call_next(request)


@app.get("/health")
def health():
    errors = []
    checks = {}
    try:
        checks["minio"] = bool(get_minio_client().bucket_exists(os.getenv("MINIO_BUCKET", "uploads")))
    except Exception as exc:
        errors.append(f"minio unavailable: {exc}")
    try:
        checks["redis"] = check_redis()
    except Exception as exc:
        errors.append(f"redis unavailable: {exc}")
    if errors:
        raise HTTPException(status_code=503, detail="; ".join(errors))
    return {"status": "ok", **checks}


@app.get("/")
def serve_ui():
    from fastapi.responses import HTMLResponse
    path = Path(__file__).with_name("app_interface.html")
    if not path.exists():
        raise HTTPException(status_code=404, detail="app_interface.html not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.post("/upload", status_code=202)
def upload_file(file: UploadFile = File(...), _auth: None = Depends(require_api_key)):
    # Deliberately a sync endpoint: MinIO put_object and the Redis round-trip
    # are blocking calls, and running them here would freeze the event loop.
    declared = (file.content_type or "application/octet-stream").lower()
    # Endpoint đồng bộ -> đọc qua file.file (SpooledTemporaryFile) thay vì
    # await file.read(); put_object/Redis bên dưới đều là lệnh chặn.
    content = file.file.read(MAX_FILE_SIZE + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_FILE_SIZE} bytes")

    mime_type = detect_mime(content, file.filename or "", declared)
    if not check_mime_allowed(mime_type):
        raise HTTPException(status_code=415, detail=f"Unsupported content type: {mime_type}")
    if mime_type in ALLOWED_IMAGE_TYPES and len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds {MAX_IMAGE_SIZE} bytes (limit {MAX_FILE_SIZE // (1024 * 1024)}MB applies to other types)",
        )

    task_id = str(uuid.uuid4())
    suffix = Path(file.filename or "upload.bin").suffix[:20]
    object_key = f"{task_id}{suffix}"
    bucket = os.getenv("MINIO_BUCKET", "uploads")
    client = get_minio_client()
    try:
        client.put_object(bucket, object_key, BytesIO(content), len(content), content_type=mime_type)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Object store unavailable: {exc}") from exc

    # Dùng cùng task_id làm Celery ID để GET /task/{task_id} tra cứu đúng task.
    # Lưu bucket + object_key thay vì URL nội bộ (người dùng không mở được
    # http://minio:9000/...); dashboard tự tạo presigned URL khi cần.
    try:
        celery_app.send_task(
            "tasks.process_file",
            args=[task_id, bucket, object_key, mime_type],
            task_id=task_id,
        )
    except Exception as exc:
        # Redis lỗi sau khi file đã lưu -> phải dọn file rác, không để mồ côi.
        log.warning("Queue unavailable for %s, rolling back object %s/%s", task_id, bucket, object_key)
        try:
            client.remove_object(bucket, object_key)
        except Exception as cleanup_exc:
            log.error("Failed to remove orphaned object %s/%s: %s", bucket, object_key, cleanup_exc)
        raise HTTPException(status_code=503, detail="Queue unavailable, upload rolled back") from exc

    return {
        "status": "accepted",
        "task_id": task_id,
        "bucket": bucket,
        "object_key": object_key,
        "mime_type": mime_type,
        "message": "File queued for processing.",
    }


@app.get("/task/{task_id}")
def get_task_status(task_id: str):
    from celery.result import AsyncResult

    result = AsyncResult(task_id, app=celery_app)
    state = result.state
    if state == "PENDING":
        # PENDING cũng là trạng thái Celery trả về cho id không tồn tại;
        # kiểm tra object gốc để phân biệt, thiếu object -> 404.
        try:
            exists = task_object_exists(task_id)
        except Exception as exc:
            log.warning("Cannot verify object for task %s, assuming queued: %s", task_id, exc)
            exists = True
        if not exists:
            raise HTTPException(status_code=404, detail="Unknown task id")
        return {"status": "queued"}
    if state in {"STARTED", "RETRY"}:
        return {"status": "processing" if state == "STARTED" else "retrying"}
    if state == "FAILURE":
        return {"status": "failed", "reason": str(result.result)}
    if state == "SUCCESS":
        return {"status": "finished", "result": result.result}
    return {"status": state.lower()}
