# Reverse-ETL runner image — ClickHouse -> Postgres upsert.
# Build context is this directory:  docker build -t ghcr.io/khaidao2/sentiment-pulse-reverse-etl:latest ./reverse_etl
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync_jobs.py .

RUN useradd --create-home etl && chown -R etl /app
USER etl

CMD ["python", "sync_jobs.py"]
