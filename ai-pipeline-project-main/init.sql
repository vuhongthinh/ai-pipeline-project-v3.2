-- Schema cho cài đặt mới: lưu bucket + object_key thay vì URL nội bộ.
-- URL http://minio:9000/... không mở được từ máy người dùng; dashboard sẽ
-- tự sinh presigned URL từ bucket + object_key.
CREATE TABLE IF NOT EXISTS metadata (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    mime_type VARCHAR(255) NOT NULL,
    short_label VARCHAR(255) NOT NULL DEFAULT 'khác',
    labels JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION,
    detail JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_metadata_created_at ON metadata (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_metadata_labels ON metadata USING GIN (labels);
CREATE INDEX IF NOT EXISTS idx_metadata_object_key ON metadata (object_key);
