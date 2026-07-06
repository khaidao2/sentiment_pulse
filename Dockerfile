# Crawler image — build from the REPO ROOT so both api_crawler/ and the Avro
# schema under data-contracts/ are in the build context:
#   docker build -t ghcr.io/khaidao2/sentiment-pulse-crawler:latest .
#
# Broker: Redpanda is Kafka-API compatible, so confluent-kafka talks to it
# unchanged — only point KAFKA_BOOTSTRAP_SERVERS at the Redpanda service.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first for layer caching. confluent-kafka and curl_cffi ship
# manylinux wheels that bundle librdkafka / libcurl-impersonate, so no apt.
COPY api_crawler/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code + the .asvc the producer loads at runtime. config.py resolves the
# schema via _REPO_ROOT (= /app) -> data-contracts/schemas/raw/itviec.asvc.
COPY api_crawler/ ./api_crawler/
COPY data-contracts/schemas/ ./data-contracts/schemas/

# Drop privileges.
RUN useradd --create-home crawler
USER crawler

CMD ["python", "-m", "api_crawler.jobs.itviec"]
