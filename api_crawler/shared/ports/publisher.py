"""Publisher port — the contract every publisher adapter must satisfy.

Zero dependencies on purpose: importing this must not drag in Kafka, config or
any framework. Adapters (KafkaPublisher, a test StdoutPublisher, ...) implement it.
"""

from abc import ABC, abstractmethod


class IPublisher(ABC):
    @abstractmethod
    def produce(self, key: str, record: dict) -> None:
        """Serialize and send one record to the sink."""

    @abstractmethod
    def flush(self, timeout: float = 30.0) -> None:
        """Block until buffered records are delivered."""
