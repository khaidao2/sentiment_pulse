"""CrawlService orchestration test — with fakes, no network and no Kafka.

Proves the Dependency Inversion payoff: the service depends only on the ports,
so we swap in a fake source (yields the saved fixture) and a fake publisher
(collects records) and exercise the whole pipeline offline.
"""

from pathlib import Path

from api_crawler.jobs.itviec.parser import ItviecParser
from api_crawler.shared.pipeline.crawl_service import CrawlService
from api_crawler.shared.ports.publisher import IPublisher
from api_crawler.shared.ports.source import ISource

_FIXTURE = Path(__file__).parent / "fixtures" / "itviec_detail.html"


class FakeSource(ISource):
    def __init__(self, pages):
        self._pages = pages

    def fetch(self):
        yield from self._pages


class FakePublisher(IPublisher):
    def __init__(self):
        self.produced = []
        self.flushed = False

    def produce(self, key, record):
        self.produced.append((key, record))

    def flush(self, timeout=30.0):
        self.flushed = True


def test_pipeline_publishes_parsed_records():
    source = FakeSource([_FIXTURE.read_text(encoding="utf-8")])
    publisher = FakePublisher()
    service = CrawlService(source, ItviecParser(), publisher, key_fn=lambda r: r["title"])

    count = service.run()

    assert count == 1
    assert publisher.flushed is True
    key, record = publisher.produced[0]
    assert key == "Network Engineer (CDS) (Open For Freshers)"
    assert record["company_name"] == "LG CNS Việt Nam"
    assert len(record["skills"]) >= 2
