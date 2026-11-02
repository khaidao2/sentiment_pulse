# Reverse-ETL runner image — ClickHouse -> Postgres upsert.
# Build context is this directory:  docker build -t ghcr.io/khaidao2/sentiment-pulse-reverse-etl:latest ./reverse_etl
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync_jobs.py sync_courses.py sync_course_skills.py backfill_skills.py backfill_requirements.py ./
# the skill dictionary + extractor; without this the image still imports
# `skills` and dies at startup
COPY skills/ ./skills/
# the course matcher (`python -m courses.match`) and its curation gate. It
# imports `skills` the same way sync_jobs.py does, so it only works from /app
# with the package beside it. Previously missing, which made the module
# unrunnable in the image at all. overrides.yaml ships INSIDE the image on
# purpose: it is reviewed in git with the code that reads it, not mounted.
COPY courses/ ./courses/

RUN useradd --create-home etl && chown -R etl /app
USER etl

CMD ["python", "sync_jobs.py"]
