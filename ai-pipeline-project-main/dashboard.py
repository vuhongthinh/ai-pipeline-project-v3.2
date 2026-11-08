import os
from datetime import timedelta

import pandas as pd
import psycopg2
import streamlit as st
from minio import Minio

PG_URL = os.getenv("PG_URL", "postgresql://user:pass@postgres:5432/pipeline")


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@st.cache_data(ttl=30)
def load_metadata(query: str):
    # Chỉ chọn cột cần thiết; tìm kiếm bằng SQL (ILIKE + toán tử JSONB) thay
    # vì kéo cột detail (gồm cả transcript) về rồi lọc bằng pandas.
    conn = psycopg2.connect(PG_URL)
    try:
        if query and query.strip():
            pattern = f"%{escape_like(query.strip())}%"
            sql = (
                "SELECT task_id, bucket, object_key, mime_type, short_label, labels, created_at "
                "FROM metadata "
                "WHERE short_label ILIKE %(pat)s "
                "   OR detail @> to_jsonb(ARRAY[%(exact)s]) "
                "   OR labels::text ILIKE %(pat)s "
                "   OR EXISTS (SELECT 1 FROM jsonb_array_elements(detail) AS e "
                "              WHERE e.value::text ILIKE %(pat)s) "
                "ORDER BY created_at DESC LIMIT 500"
            )
            params = {"pat": pattern, "exact": query.strip()}
        else:
            sql = (
                "SELECT task_id, bucket, object_key, mime_type, short_label, labels, created_at "
                "FROM metadata ORDER BY created_at DESC LIMIT 500"
            )
            params = None
        df = pd.read_sql_query(sql, conn, params=params)
        total = pd.read_sql_query("SELECT count(*) AS n FROM metadata", conn)["n"].iloc[0]
        return df, int(total)
    finally:
        conn.close()


def get_presign_client():
    """Client dùng riêng để KÝ URL.

    Chữ ký SigV4 tính cả header Host, nên phải ký bằng endpoint mà TRÌNH DUYỆT
    người dùng sẽ truy cập (ví dụ localhost:9000 từ máy Windows), không phải
    hostname nội bộ "minio:9000" mà container mới hiểu. Thay hostname sau khi
    ký sẽ làm hỏng chữ ký, nên endpoint công khai phải được dùng ngay từ đầu.
    """
    return Minio(
        os.getenv("MINIO_PUBLIC_URL", "localhost:9000"),
        access_key=os.getenv("MINIO_ROOT_USER", "minioadmin"),
        secret_key=os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"),
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
        # Ghim region để presign tính toán thuần local (không gọi network hỏi
        # region của bucket) - MinIO mặc định chấp nhận scope us-east-1.
        region=os.getenv("MINIO_REGION", "us-east-1"),
    )


def build_presigned_links(df: pd.DataFrame) -> list[str]:
    """file_url nội bộ (http://minio:9000/...) người dùng không mở được; tạo
    presigned URL ngắn hạn cho từng dòng hiển thị."""
    links = []
    try:
        client = get_presign_client()
    except Exception:
        return [""] * len(df)
    for _, row in df.iterrows():
        try:
            url = client.presigned_get_object(row["bucket"], row["object_key"], expires=timedelta(hours=1))
        except Exception:
            url = ""
        links.append(url)
    return links


def main():
    st.set_page_config(page_title="Multimodal Data Explorer", layout="wide")
    st.title("Multimodal Data Explorer")

    query = st.text_input("Tìm theo nhãn, loại tệp hoặc nội dung (transcript)")
    try:
        view, total = load_metadata(query or "")
    except Exception as exc:
        st.error(f"Không thể đọc PostgreSQL: {exc}")
        st.stop()

    if view.empty:
        st.info("Chưa có dữ liệu khớp. Hãy upload file qua API.")
        st.stop()

    view = view.copy()
    if {"bucket", "object_key"}.issubset(view.columns):
        view["file_link"] = build_presigned_links(view)

    c1, c2, c3 = st.columns(3)
    c1.metric("Tổng bản ghi", total)
    c2.metric("Đang hiển thị", len(view))
    c3.metric("Loại dữ liệu", int(view["mime_type"].nunique()))

    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "file_link": st.column_config.LinkColumn("Tệp (link 1 giờ)", display_text="Mở tệp"),
            "labels": st.column_config.ListColumn("Labels"),
        },
    )


# Streamlit chạy script với __name__ == "__main__", nên guard này vẫn hoạt
# động khi `streamlit run dashboard.py`, đồng thời cho phép import an toàn
# trong unit tests.
if __name__ == "__main__":
    main()
