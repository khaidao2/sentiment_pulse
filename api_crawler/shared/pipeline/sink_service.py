"""Generic sink orchestration: subscriber -> sink, with batched commits.

The inverse of CrawlService. Depends only on the port abstractions
(ISubscriber, ISink), never on Kafka or MinIO — so it is testable with fakes and
reused by every topic. Long-running: it loops until SIGTERM/SIGINT (the signal a
Kubernetes Deployment gets on rollout), then flushes the remainder and commits so
no buffered record is lost.

Delivery is at-least-once: offsets are committed only after the sink has durably
written the batch. A crash between flush and commit re-delivers the last batch —
acceptable for a raw landing zone.
"""

from __future__ import annotations

import logging
import signal

from api_crawler.shared.ports.sink import ISink
from api_crawler.shared.ports.subscriber import ISubscriber

_LOG = logging.getLogger(__name__)


class SinkService:
    def __init__(self, subscriber: ISubscriber, sink: ISink, poll_timeout: float = 1.0) -> None:
        self._subscriber = subscriber
        self._sink = sink
        self._poll_timeout = poll_timeout
        self._running = True

    def _stop(self, *_: object) -> None:
        _LOG.info("shutdown signal received | draining buffer")
        self._running = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        try:
            while self._running:
                record = self._subscriber.poll(self._poll_timeout)
                if record is not None:
                    self._sink.add(record)
                if self._sink.should_flush():
                    if self._sink.flush():
                        self._subscriber.commit()
        finally:
            if self._sink.close():
                self._subscriber.commit()
            self._subscriber.close()
            _LOG.info("sink stopped cleanly")
