# Multimodal AI Ingestion Pipeline (local development)

**V3.2** — tuned for CPU-only laptops. See the changelog at the bottom.

## What it does
Receives image, audio, and text uploads through FastAPI; stores originals in MinIO; queues asynchronous processing with Celery/Redis; uses Ollama for image/text analysis and a Whisper service for audio transcription; persists normalized results (bucket + object key) in PostgreSQL; displays recent records with presigned download links in Streamlit.

## Prerequisites
- Docker Desktop with Docker Compose v2
- **CPU-only laptops work with the defaults** (~6 GB RAM free is a practical minimum). Any machine with 16 GB+ can upgrade models via `.env`.
- Disk space for container images and downloaded models (several GB).

## Start
1. Copy `.env.example` to `.env`. **The two passwords are mandatory**: compose refuses to start (`${POSTGRES_PASSWORD:?...}` / `${MINIO_ROOT_PASSWORD:?...}`) instead of silently falling back to a known default.
2. Start services: `docker compose up --build -d`.
   - On first boot the `ollama-init` service pulls the configured models (skipped when already cached in the volume). `api` and `worker` wait for it, so tasks never fail with a confusing "model not found" 404. Follow progress: `docker compose logs -f ollama-init`.
3. Check `http://localhost:8000/health` (reports both MinIO **and** Redis), API docs at `http://localhost:8000/docs`, dashboard at `http://localhost:8501`, MinIO console at `http://localhost:9001`.
4. Upload a small `.txt` file first. Then test an image. Test audio only after Whisper has finished downloading/loading its model (`docker compose logs -f whisper`).
5. Optional demo bot: `docker compose --profile demo up -d clawbot` (not started by default).

## CPU-only laptop defaults
| Component | Default (CPU) | Heavier option (`.env`) |
| --- | --- | --- |
| Text model | `llama3.2:1b` | `llama3.2:3b`, `qwen2.5:3b` |
| Vision model | `moondream` | `llava:7b`, `qwen2.5vl:3b`, `gemma3:4b` |
| Whisper | `base` (+ `language=vi`) | `small` (better Vietnamese, slower) |
| Ollama | `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `keep_alive=5m` | raise in `.env` |

`OLLAMA_NUM_CTX` stays 8192 (Vietnamese is token-heavy; lower to 4096 only if RAM is tight). Commented `deploy.resources.limits` examples are in `docker-compose.yml`.

## Important development notes
- The MinIO server image comes from `quay.io/minio/minio` because MinIO no longer publishes the community image on Docker Hub.
- **Presigned URLs are signed with `MINIO_PUBLIC_URL`** (default `localhost:9000`), not the container-internal `minio:9000`. SigV4 signs the Host header, so signing must use the endpoint the *browser* will call; rewriting the hostname afterwards invalidates the signature. If you open the dashboard from another machine, set `MINIO_PUBLIC_URL` to the Docker host's IP/hostname. Region is pinned (`us-east-1`) so signing is a pure local computation.
- MIME types are detected from file content (magic bytes via `filetype`), with the filename extension as fallback; the client-declared header is only the last resort. Images are whitelisted to JPEG/PNG/WEBP only — SVG/HEIC/TIFF are rejected with 415 before reaching the vision model. Images are capped at `MAX_IMAGE_SIZE` (10 MB) and downscaled to a ~1024 px long edge before base64-encoding into the VLM request.
- **Outage handling**: if Redis fails after the object was stored, the API rolls the upload back (deletes the object) and returns 503 — no orphaned files. If MinIO is down, uploads return 503 and never queue tasks. `/health` checks both stores. Oversized `/upload` requests are rejected at the HTTP layer by a Content-Length middleware before the body is read.
- `/task/{id}` returns **404 for ids that were never uploaded** (no matching object in MinIO), instead of a vague `queued` for unknown ids.
- **Optional API key**: set `API_KEY` in `.env` and callers must send `X-API-Key` on `/upload` (the demo clawbot sends it automatically). Leave it empty for the web UI, which cannot set headers. `/task/{id}` and `/health` stay open in local dev.
- Prompts are sent through `/api/chat` with instructions in the system message and file content in the user message (reduces prompt-injection risk), with explicit `options.num_ctx` so long Vietnamese text is not silently truncated, and `keep_alive` to balance warmth vs RAM.
- Task errors are classified: HTTP 400/404/413/415/422 (bad input, missing model, wrong format) fail immediately; timeouts, 429/5xx, and connection errors retry (max 2, 30 s delay). The DB pool is created lazily inside each worker process, so the API can start before Postgres and `--concurrency > 1` is safe.
- Results are stored as `bucket` + `object_key`; the dashboard generates 1-hour presigned URLs. The dashboard queries only the columns it shows and filters in SQL (`ILIKE` + JSONB containment on the GIN index).
- **Upgrading an old database**: back it up, then run `docker compose exec -T postgres psql -U pipeline_user -d pipeline < migrate_legacy.sql`. The migration backfills `bucket`/`object_key` from `file_url`, makes `file_url` nullable (the new worker code doesn't write it — old NOT NULL would reject every insert), keeps historical data, and is idempotent. Verified against a legacy schema (`task_id UUID`, `file_url NOT NULL`) by `tests/test_migration.py` on a real Postgres.
- This is a development/portfolio baseline, not production-hardened. Add TLS, real secret management, rate limiting, and monitoring before any public deployment.

## Tests
```
pip install -r requirements-dev.txt -r requirements-api.txt -r requirements-worker.txt
python -m pytest tests/ -v
```
Covers: `parse_json_object`, `as_string_list`, lazy DB pool, image shrinking, Ollama chat payload, `/upload` MIME detection/limits/whitelist (fake MinIO + fake Celery), **Redis-outage rollback**, **MinIO-outage 503**, **health outage reporting**, **404 for unknown tasks**, **API key on/off**, **presign signed with the public endpoint**, clawbot 202/API-key handling, and **migration on a real legacy Postgres schema** (via `pgserver`, auto-skipped if unavailable).

## Current limitations
- Accepts JPEG/PNG/WEBP images, audio, plain text, and JSON only. PDF/OCR and video are not implemented.
- `confidence` is nullable; no calibrated confidence score is claimed.
- Text is truncated to a bounded prompt length (12,000 chars). Large-file chunking and evaluation datasets are future work.
- `llama3.2` is English-leaning and Vietnamese is not among its official languages; `whisper base` is weak for Vietnamese. Evaluate on your own 50–100 samples before trusting outputs (see roadmap).
- A finished task whose Celery result expired (24 h) shows as `queued` again in `/task/{id}` while the original object still exists.
- The sample `mini_clawbot.py` generates synthetic market text; it is not a real crawler and should not be described as one.

## V3.2 changelog
1. **Migration compatibility (P0)**: `migrate_legacy.sql` now drops the `NOT NULL` constraint on the legacy `file_url` column (kept for history) — previously every new insert was rejected on old databases. Restored the `task_id` UUID→TEXT cast. Tested on a real legacy schema, twice (idempotency).
2. **Presigned URL fix (P0)**: dashboard signs with `MINIO_PUBLIC_URL` (browser-reachable endpoint); `MINIO_URL` remains the internal one. Region pinned to avoid a network round-trip per signature.
3. **CPU-only compose**: default models downsized (`llama3.2:1b`, `moondream`, whisper `base`), Ollama restricted to one loaded model / one parallel request, shorter `keep_alive`, Redis capped at 256 MB, commented memory-limit examples.
4. **Redis-outage cleanup**: uploads that fail at queue time delete the stored object and return 503 (was: orphaned file + 500).
5. **`/task/{id}` 404** for never-existing task ids (MinIO prefix check) + UI stops polling on 404.
6. **Optional `X-API-Key` auth** on `/upload` (off by default; clawbot supports it).
7. **Health check now covers Redis** in addition to MinIO; Content-Length middleware rejects oversized uploads early.
8. **Test suite expanded to 56 tests**, including real-Postgres migration tests and an end-to-end worker-to-Postgres integration test (`pgserver`).

## Suggested roadmap (after V3.2)
1. ~~Unit tests for parsing, MIME handling, migration~~ (done — 56 tests).
2. Integration tests against real Redis/MinIO containers (e.g. testcontainers); upload smoke tests for all three formats.
3. Idempotency keys, task-state table (distinguish expired results reliably), dead-letter queue.
4. Full auth (JWT/user accounts, per-endpoint scopes), rate limiting.
5. Data engineering: input validation reports, ETL, data-quality dashboards.
6. AI engineering: evaluation set (50–100 real samples), compare 2–3 text/vision models, then chunking/embeddings/RAG.
7. Observability: structured logs, Prometheus/Grafana metrics, traces.
8. Portfolio polish: English docs, architecture diagram, demo video.
