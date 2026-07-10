# dbt runner image — dbt-clickhouse + the sentpul dbt project baked in.
# Build context is this dbt/ directory:
#   docker build -t ghcr.io/khaidao2/sentiment-pulse-dbt:latest ./dbt
#
# Runtime config comes from env vars (see profiles.yml): CLICKHOUSE_HOST,
# CLICKHOUSE_PASSWORD, MINIO_ACCESS_KEY, MINIO_SECRET_KEY. The Airflow DAG (and
# the one-off test pod) inject them.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DBT_PROFILES_DIR=/dbt

WORKDIR /dbt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /dbt/

# Drop privileges; dbt writes target/ and logs/ under /dbt.
RUN useradd --create-home dbt && chown -R dbt /dbt
USER dbt

CMD ["dbt", "build"]
