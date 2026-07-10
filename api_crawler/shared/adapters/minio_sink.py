"""MinIO (S3) sink — buffers records and lands them as date-partitioned Parquet.

Layout on the bucket:
    s3://<bucket>/<prefix>/dt=YYYY-MM-DD/part-<ts>-<uuid>.parquet

Parquet is columnar and compresses well, so ClickHouse can read it back with the
s3() table function. An explicit Arrow `schema` (derived from the Avro schema by
the composition root) keeps column types stable even when a batch is all-null;
without one, types are inferred per batch. Flush happens by record count or age,
whichever comes first; partitioning uses the ingest date (UTC).

This is an ISink adapter — that port is the seam. To add another store (GCS,
S3, ...) later, write a sibling ISink adapter; only extract a shared
IObjectStore then, once a second backend actually exists.
"""

from __future__ import annotations

import io
import logging
import time
import uuid
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
from minio import Minio

from api_crawler.shared.ports.sink import ISink

_LOG = logging.getLogger(__name__)


class MinioSink(ISink):
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str,
        max_records: int = 500,
        max_seconds: float = 60.0,
        schema: pa.Schema | None = None,
    ) -> None:
        secure = endpoint.startswith("https://")
        host = endpoint.split("://", 1)[-1].rstrip("/")
        self._client = Minio(host, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._max_records = max_records
        self._max_seconds = max_seconds
        self._schema = schema
        self._buffer: list[dict] = []
        self._last_flush = time.monotonic()

        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)
            _LOG.info("Created bucket %s", bucket)
        _LOG.info(
            "MinioSink ready | bucket=%s | prefix=%s | batch=%d/%.0fs",
            bucket, self._prefix, max_records, max_seconds,
        )

    def add(self, record: dict) -> None:
        self._buffer.append(record)

    def should_flush(self) -> bool:
        if not self._buffer:
            return False
        return (
            len(self._buffer) >= self._max_records
            or (time.monotonic() - self._last_flush) >= self._max_seconds
        )

    def flush(self) -> int:
        if not self._buffer:
            return 0
        records, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()

        table = pa.Table.from_pylist(records, schema=self._schema)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="snappy")
        payload = buf.getvalue()

        now = datetime.now(timezone.utc)
        key = f"{self._prefix}/dt={now:%Y-%m-%d}/part-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.parquet"
        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(payload),
            length=len(payload),
            content_type="application/vnd.apache.parquet",
        )
        _LOG.info("Flushed %d record(s) -> s3://%s/%s", len(records), self._bucket, key)
        return len(records)

    def close(self) -> int:
        return self.flush()
