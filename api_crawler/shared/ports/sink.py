"""Sink port — the contract every record-store adapter must satisfy.

Mirror of IPublisher, but for landing records into object storage / a warehouse.
The pipeline buffers records into the sink and flushes in batches; the sink
decides the on-disk format (Parquet, NDJSON, ...) and layout. Zero dependencies.
"""

from abc import ABC, abstractmethod


class ISink(ABC):
    @abstractmethod
    def add(self, record: dict) -> None:
        """Buffer one record for the next flush."""

    @abstractmethod
    def should_flush(self) -> bool:
        """True when the buffer should be written out (size or age threshold)."""

    @abstractmethod
    def flush(self) -> int:
        """Write the buffered records out as one object. Returns rows written."""

    @abstractmethod
    def close(self) -> int:
        """Flush any remainder and release resources. Returns rows written."""
