"""Generic crawl orchestration: source -> parser -> publisher.

Depends only on the port abstractions (ISource, IParser, IPublisher), never on
concrete adapters — so the same service runs any source and is testable with
fakes (no network, no Kafka). This is the Dependency Inversion payoff.
"""

from __future__ import annotations

import logging
from typing import Callable

from api_crawler.shared.ports.parser import IParser
from api_crawler.shared.ports.publisher import IPublisher
from api_crawler.shared.ports.source import ISource

_LOG = logging.getLogger(__name__)


class CrawlService:
    def __init__(
        self,
        source: ISource,
        parser: IParser,
        publisher: IPublisher,
        key_fn: Callable[[dict], str],
    ) -> None:
        self._source = source
        self._parser = parser
        self._publisher = publisher
        self._key_fn = key_fn

    def run(self) -> int:
        """Fetch every page, parse to records, publish each. Returns the count."""
        count = 0
        for page in self._source.fetch():
            for record in self._parser.parse(page):
                self._publisher.produce(self._key_fn(record), record)
                count += 1
        self._publisher.flush()
        _LOG.info("crawl complete | %d records published", count)
        return count
