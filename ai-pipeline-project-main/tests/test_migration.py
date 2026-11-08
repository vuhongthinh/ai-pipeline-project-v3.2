"""Kiểm thử migration trên database CÓ THẬT với schema legacy thật.

Dùng pgserver (Postgres thật đóng gói trong wheel) để:
  1. Dựng schema cũ (task_id UUID, file_url TEXT NOT NULL, không có
     bucket/object_key).
  2. Chạy migrate_legacy.sql (2 lần - kiểm tra idempotency).
  3. Chạy INSERT kiểu tasks.py mới (không truyền file_url) - phải thành công
     sau khi migration đã DROP NOT NULL cho file_url.

Test tự skip nếu pgserver chưa cài (xem requirements-dev.txt).

Chạy: python -m pytest tests/test_migration.py -v
"""
import pathlib

import pytest  # noqa: E402

pgserver = pytest.importorskip("pgserver", reason="cần pgserver (requirements-dev.txt)")
psycopg2 = pytest.importorskip("psycopg2")

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION_SQL = (PROJECT_ROOT / "migrate_legacy.sql").read_text(encoding="utf-8")
INIT_SQL = (PROJECT_ROOT / "init.sql").read_text(encoding="utf-8")

# Schema legacy được suy ra từ migrate_legacy.sql gốc: task_id từng là UUID,
# file_url bắt buộc, chưa có bucket/object_key/mime_type/short_label/detail.
LEGACY_SCHEMA = """
CREATE TABLE metadata (
    id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    file_url TEXT NOT NULL,
    labels JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

LEGACY_ROWS = """
INSERT INTO metadata (task_id, file_url, labels) VALUES
  (gen_random_uuid(), 'http://minio:9000/uploads/anh-cho.png', '["chó", "cỏ"]'::jsonb),
  (gen_random_uuid(), 'http://minio:9000/uploads/video-quang-cao.mp3', '[]'::jsonb);
"""

NEW_STYLE_INSERT = """
INSERT INTO metadata (task_id, bucket, object_key, mime_type, short_label, labels, confidence, detail)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
"""


@pytest.fixture(scope="module")
def server():
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pg-mig-v32-") as data_dir:
        with pgserver.get_server(data_dir) as srv:
            yield srv


@pytest.fixture()
def fresh_db(server):
    """Mỗi test một database riêng để không ảnh hưởng nhau."""
    import uuid

    dbname = f"mig_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(server.get_uri())
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE {dbname}')
    admin.close()

    conn = psycopg2.connect(server.get_uri(dbname))
    yield conn
    conn.close()
    admin = psycopg2.connect(server.get_uri())
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE {dbname}')
    admin.close()


def run_sql(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def query_one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


class TestLegacyMigration:
    def test_migration_backfills_and_relaxes_file_url(self, fresh_db):
        conn = fresh_db
        run_sql(conn, LEGACY_SCHEMA)
        run_sql(conn, LEGACY_ROWS)

        # Trước migration: file_url bắt buộc, chưa có bucket/object_key.
        nullable = query_one(conn, """SELECT is_nullable FROM information_schema.columns
                                      WHERE table_name='metadata' AND column_name='file_url'""")
        assert nullable[0] == "NO"

        # Chạy migration, rồi chạy lần NỮA để kiểm chứng idempotency.
        run_sql(conn, MIGRATION_SQL)
        run_sql(conn, MIGRATION_SQL)

        # file_url vẫn còn (giữ lịch sử) nhưng không còn NOT NULL.
        nullable = query_one(conn, """SELECT is_nullable, data_type FROM information_schema.columns
                                      WHERE table_name='metadata' AND column_name='file_url'""")
        assert nullable[0] == "YES"

        # task_id được ép về TEXT.
        task_type = query_one(conn, """SELECT data_type FROM information_schema.columns
                                       WHERE table_name='metadata' AND column_name='task_id'""")
        assert task_type[0] == "text"

        # Dữ liệu legacy được backfill bucket + object_key đúng.
        row1 = query_one(conn, """SELECT bucket, object_key, mime_type, short_label
                                  FROM metadata WHERE object_key = 'anh-cho.png'""")
        assert row1 == ("uploads", "anh-cho.png", "application/octet-stream", "chó")
        row2 = query_one(conn, """SELECT bucket, object_key, labels
                                  FROM metadata WHERE object_key = 'video-quang-cao.mp3'""")
        assert row2[0] == "uploads" and row2[1] == "video-quang-cao.mp3"

        # file_url lịch sử vẫn nguyên vẹn (không xóa cột).
        old_url = query_one(conn, "SELECT file_url FROM metadata WHERE object_key = 'anh-cho.png'")
        assert old_url[0] == "http://minio:9000/uploads/anh-cho.png"

        # Các index mới đều đã có.
        idx = query_one(conn, """SELECT count(*) FROM pg_indexes
                                 WHERE tablename='metadata'
                                   AND indexname IN ('idx_metadata_labels',
                                                     'idx_metadata_created_at',
                                                     'idx_metadata_object_key',
                                                     'idx_metadata_task_id_unique')""")
        assert idx[0] == 4

    def test_new_style_insert_after_migration(self, fresh_db):
        """Bug 1 của V2: tasks.py mới INSERT không truyền file_url - sau migration
        phải thành công, không bị NOT NULL từ chối."""
        conn = fresh_db
        run_sql(conn, LEGACY_SCHEMA)
        run_sql(conn, LEGACY_ROWS)
        run_sql(conn, MIGRATION_SQL)

        run_sql(conn, NEW_STYLE_INSERT, (
            "task-moi-123", "uploads", "task-moi-123.png", "image/png",
            "con vật", '["chó"]', None, '[{"type": "image"}]',
        ))
        row = query_one(conn, """SELECT bucket, object_key, file_url
                                 FROM metadata WHERE task_id = 'task-moi-123'""")
        assert row[0] == "uploads" and row[1] == "task-moi-123.png"
        assert row[2] is None  # bản ghi mới không có file_url - đúng chủ đích


class TestFreshInstall:
    def test_init_sql_schema_accepts_new_inserts(self, fresh_db):
        """Cài mới: init.sql không còn file_url, INSERT kiểu mới hoạt động ngay."""
        conn = fresh_db
        run_sql(conn, INIT_SQL)
        run_sql(conn, NEW_STYLE_INSERT, (
            "task-fresh-1", "uploads", "task-fresh-1.jpg", "image/jpeg",
            "bãi biển", '["biển"]', None, '[{"type": "image"}]',
        ))
        columns = query_one(conn, """SELECT count(*) FROM information_schema.columns
                                     WHERE table_name='metadata' AND column_name='file_url'""")
        assert columns[0] == 0  # schema mới không có cột legacy

        row = query_one(conn, "SELECT bucket, object_key FROM metadata WHERE task_id='task-fresh-1'")
        assert row == ("uploads", "task-fresh-1.jpg")
