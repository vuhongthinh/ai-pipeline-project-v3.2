import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

# Mặc định nhẹ cho laptop CPU-only; "small" cho chất lượng tiếng Việt tốt hơn
# nếu chấp nhận chậm hơn. Đặt WHISPER_LANGUAGE= (rỗng) để tự dò ngôn ngữ.
MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
# Vietnamese audio is common here; set WHISPER_LANGUAGE="" to auto-detect.
# "base" struggles with Vietnamese - prefer small or larger.
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "vi") or None
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_MB", "50")) * 1024 * 1024

model = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import whisper

    global model
    model = whisper.load_model(MODEL_NAME)
    yield


app = FastAPI(title="Whisper transcription service", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "loaded": model is not None}


@app.post("/transcribe")
def transcribe(file: UploadFile = File(...)):
    # Sync on purpose: model.transcribe() is CPU-heavy and blocks for minutes,
    # so it must run in FastAPI's threadpool, never on the event loop.
    if model is None:
        raise HTTPException(status_code=503, detail="Whisper model is not loaded")
    suffix = Path(file.filename or "audio.bin").suffix or ".bin"
    # Endpoint đồng bộ: đọc qua file.file thay vì await file.read().
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio file")
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail=f"Audio file exceeds {MAX_AUDIO_BYTES // (1024 * 1024)}MB")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            temp_path = tmp.name
        result = model.transcribe(temp_path, language=WHISPER_LANGUAGE)
        return {"text": result.get("text", "").strip(), "language": result.get("language")}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Transcription failed: {exc}") from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
