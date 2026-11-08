"""Integration test: đường đi của worker với Postgres THẬT.

Chạy process_file (celery task thật) từ đầu đến cuối: lazy DB pool ->
process -> save_metadata vào PostgreSQL thật (pgserver). Chỉ giả lập
download_object (MinIO) và ask_ollama (LLM) vì không thể chạy hai dịch vụ đó
trong môi trường test.

Chạy: python -m pytest tests/test_worker_integration.py -v
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

pgserver = pytest.importorskip("pgserver", reason="cần pgserver (requirements-dev.txt)")

import tasks  # noqa: E402


@pytest.fixture(scope="module")
def server():
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pg-worker-v32-") as data_dir:
        with pgserver.get_server(data_dir) as srv:
            yield srv


@pytest.fixture()
def pg_env(server, monkeypatch):
    import uuid

    dbname = f"worker_{uuid.uuid4().hex[:8]}"
    admin = __import__("psycopg2").connect(server.get_uri())
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {dbname}")
    admin.close()

    monkeypatch.setenv("PG_URL", server.get_uri(dbname))
    tasks._db_pool = None  # pool phải được tạo lười trên URI mới

    # Schema mới
    init_sql = (pathlib.Path(__file__).resolve().parent.parent / "init.sql").read_text(encoding="utf-8")
    conn = tasks.get_db_pool().getconn()
    with conn, conn.cursor() as cur:
        cur.execute(init_sql)
    tasks.get_db_pool().putconn(conn)

    yield server.get_uri(dbname)

    # Dọn dẹp pool + database
    if tasks._db_pool is not None:
        tasks._db_pool.closeall()
        tasks._db_pool = None
    import psycopg2

    admin = psycopg2.connect(server.get_uri())
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE {dbname}")
    admin.close()


@pytest.fixture()
def fake_services(monkeypatch):
    monkeypatch.setattr(
        tasks, "download_object",
        lambda bucket, key: "dữ liệu tiếng Việt: thị trường tăng mạnh, Bitcoin vượt mốc kháng cự".encode("utf-8"),
    )
    monkeypatch.setattr(
        tasks,
        "ask_ollama",
        lambda model, system_prompt, user_content, images_b64=None: '{"sentiment":"positive","labels":["bitcoin","th\u1ecb tr\u01b0\u1eddng"]}',
    )


class TestWorkerIntegration:
    def test_text_task_end_to_end_persists_to_postgres(self, pg_env, fake_services):
        payload = tasks.process_file.run(
            "task-int-1", "uploads", "task-int-1.txt", "text/plain"
        )
        assert payload["short_label"]
        assert "bitcoin" in payload["labels"]

        import psycopg2

        conn = psycopg2.connect(pg_env)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bucket, object_key, mime_type, short_label, labels, detail "
                "FROM metadata WHERE task_id = %s",
                ("task-int-1",),
            )
            row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "uploads" and row[1] == "task-int-1.txt"
        assert row[2] == "text/plain"
        assert "bitcoin" in row[4]  # JSONB labels
        assert row[5][0]["type"] == "text"

    def test_idempotent_save_upserts_same_task(self, pg_env, fake_services):
        tasks.process_file.run("task-int-2", "uploads", "task-int-2.txt", "text/plain")
        tasks.process_file.run("task-int-2", "uploads", "task-int-2.txt", "text/plain")

        import psycopg2

        conn = psycopg2.connect(pg_env)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM metadata WHERE task_id = %s", ("task-int-2",))
            count = cur.fetchone()[0]
        conn.close()
        assert count == 1  # ON CONFLICT DO UPDATE, không nhân đôi bản ghi

    def test_unsupported_mime_fails_without_db_writes(self, pg_env, fake_services):
        with pytest.raises(ValueError):
            tasks.process_file.run("task-int-3", "uploads", "x.bin", "application/x-unknown")

        import psycopg2

        conn = psycopg2.connect(pg_env)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM metadata WHERE task_id = %s", ("task-int-3",))
            count = cur.fetchone()[0]
        conn.close()
        assert count == 0
