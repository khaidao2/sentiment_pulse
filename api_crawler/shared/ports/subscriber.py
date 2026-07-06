"""Subscriber port — the contract every Kafka-consuming adapter must satisfy.

Mirror of ISource, but for the sink side: instead of fetching raw pages from the
web, it pulls decoded records off a topic. Zero dependencies on purpose — the
pipeline depends on this ABC, never on confluent_kafka.
"""

from abc import ABC, abstractmethod


class ISubscriber(ABC):
    @abstractmethod
    def poll(self, timeout: float = 1.0) -> dict | None:
        """Return one decoded record, or None if nothing arrived within timeout."""

    @abstractmethod
    def commit(self) -> None:
        """Commit consumed offsets — call only after records are durably sunk."""

    @abstractmethod
    def close(self) -> None:
        """Leave the consumer group cleanly."""
