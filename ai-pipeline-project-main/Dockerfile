FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# psycopg2-binary không cần gcc/libpq-dev.
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

# COPY có chọn lọc thay vì COPY . .
COPY main.py celery_app.py app_interface.html ./

RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
