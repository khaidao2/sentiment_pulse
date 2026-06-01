# Image for the vn_stock data pipeline: crawler (producer) and sink.
# Built & pushed to ghcr.io by .github/workflows/build-image.yml.
FROM python:3.12-slim

WORKDIR /app

# Runtime deps: crawler (vnstock, pandas) + generated producer/sink
# (kafka-python, fastavro, clickhouse-connect, prometheus-client).
COPY api_crawler/vn_stock/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Application code — api_crawler.* importable from /app.
COPY api_crawler/ /app/api_crawler/

# Default: run the sink (long-running Kafka -> ClickHouse consumer).
# Override for the crawler:  python -m api_crawler.vn_stock.src
CMD ["python", "/app/api_crawler/vn_stock/sink.py"]
