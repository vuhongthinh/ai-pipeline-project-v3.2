-- Run once against an existing database created with an older project schema.
-- Back up the database before running this migration.
-- Idempotent: chạy lại nhiều lần không gây lỗi.

-- Bước 1: các cột từ bản legacy đầu tiên.
-- task_id cũ có thể là UUID: ép về TEXT để khớp schema mới.
ALTER TABLE metadata ALTER COLUMN task_id TYPE TEXT USING task_id::TEXT;
ALTER TABLE metadata ADD COLUMN IF NOT EXISTS mime_type VARCHAR(255) NOT NULL DEFAULT 'application/octet-stream';
ALTER TABLE metadata ADD COLUMN IF NOT EXISTS short_label VARCHAR(255) NOT NULL DEFAULT 'khác';
ALTER TABLE metadata ADD COLUMN IF NOT EXISTS detail JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE metadata ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
UPDATE metadata
SET short_label = COALESCE(NULLIF(labels->>0, ''), short_label)
WHERE jsonb_typeof(labels) = 'array' AND jsonb_array_length(labels) > 0;

-- Bước 2: chuyển file_url (URL nội bộ) thành bucket + object_key.
ALTER TABLE metadata ADD COLUMN IF NOT EXISTS bucket TEXT;
ALTER TABLE metadata ADD COLUMN IF NOT EXISTS object_key TEXT;

UPDATE metadata
SET bucket = split_part(path_part, '/', 1),
    object_key = regexp_replace(path_part, '^[^/]+/', '')
FROM (
    SELECT id, regexp_replace(file_url, '^https?://[^/]+/', '') AS path_part
    FROM metadata
    WHERE (bucket IS NULL OR object_key IS NULL) AND file_url IS NOT NULL AND file_url <> ''
) AS src
WHERE metadata.id = src.id;

-- Bước 3: tasks.py mới INSERT không còn ghi file_url. Database cũ khai báo
-- file_url TEXT NOT NULL nên sẽ từ chối mọi bản ghi mới; bỏ ràng buộc NOT NULL
-- (KHÔNG xóa cột) để giữ dữ liệu lịch sử sau khi đã backfill ở bước 2.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'metadata'
          AND column_name = 'file_url'
    ) THEN
        ALTER TABLE metadata ALTER COLUMN file_url DROP NOT NULL;
    END IF;
END $$;

ALTER TABLE metadata ALTER COLUMN bucket SET NOT NULL;
ALTER TABLE metadata ALTER COLUMN object_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_metadata_task_id_unique ON metadata(task_id);
CREATE INDEX IF NOT EXISTS idx_metadata_created_at ON metadata(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_metadata_labels ON metadata USING GIN(labels);
CREATE INDEX IF NOT EXISTS idx_metadata_object_key ON metadata(object_key);
