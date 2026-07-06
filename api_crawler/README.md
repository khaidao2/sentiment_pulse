# api_crawler

Crawlers that fetch job/news/community pages, parse them into records, and
publish Avro-encoded messages to Kafka (or any Kafka-API broker, e.g. Redpanda).
Built on Clean Architecture / Ports & Adapters so a new source reuses the shared
pipeline and only adds its own `source` + `parser`.

## Layout

```
api_crawler/
├── shared/
│   ├── ports/          # zero-dep ABCs: ISource, IParser, IPublisher
│   ├── adapters/       # AvroKafkaProducer (Avro + Apicurio ccompat wire format)
│   ├── pipeline/       # CrawlService: source -> parser -> publisher
│   └── config.py       # cluster-wide env: KAFKA_BOOTSTRAP_SERVERS, SCHEMA_REGISTRY_URL
└── jobs/
    └── itviec/         # one source
        ├── source.py   # 2-level crawl (listing -> detail), curl_cffi past Cloudflare
        ├── parser.py   # detail HTML -> record dict (CSS + schema.org JSON-LD)
        ├── config.py   # per-source env (dev fallbacks; prod values injected by the DAG)
        └── __main__.py # composition root: wire adapters, run CrawlService
```

The dependency rule points inward: `ports/` depend on nothing, adapters/pipeline
depend on ports, and `__main__` is the only place that knows the concrete
classes. `CrawlService` is testable offline with fake source/publisher.

## Config

Runtime config comes from the environment. In production the
sent-gen-generated Airflow DAG injects it from the contract YAML
(`data-contracts/contracts/producer/itviec.yaml`) — that YAML is the single
source of truth. The crawler never parses YAML.

| Env var                   | From contract            | Dev default                         |
| ------------------------- | ------------------------ | ----------------------------------- |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka.bootstrap_servers`| `kafka.kafka.svc.cluster.local:9092`|
| `SCHEMA_REGISTRY_URL`     | (in-cluster default)     | Apicurio `/apis/ccompat/v6`         |
| `KAFKA_TOPIC`             | `kafka.topic`            | `sentiment-pulse.itviec`            |
| `LISTING_URL`             | `crawler.listing_url`    | `https://itviec.com/it-jobs`        |
| `MAX_PAGES`               | `crawler.max_pages`      | `5`                                 |
| `DELAY`                   | `crawler.delay`          | `0.5` (seconds between detail fetches) |

## Build the image

Build from the **repo root** (the context needs both `api_crawler/` and the
`.avsc` under `data-contracts/`):

```bash
docker build -t ghcr.io/khaidao2/sentiment-pulse-crawler:latest .
```

The image does not include `sentpul` — the producer serializes with `fastavro`
directly. Deps are pinned in `api_crawler/requirements.txt`.

## Run locally against Redpanda

Redpanda is Kafka-API compatible, so the same image and code run against it —
only the broker address changes. Its built-in Schema Registry (`:8081`) is
Confluent-compatible, so no separate Apicurio is needed for local testing.

```bash
# 1. broker + schema registry in one container
docker run -d --name redpanda -p 9092:9092 -p 8081:8081 \
  redpandadata/redpanda redpanda start --mode dev-container

# 2. crawl one listing page into it
docker run --rm --network host \
  -e KAFKA_BOOTSTRAP_SERVERS=localhost:9092 \
  -e SCHEMA_REGISTRY_URL=http://localhost:8081 \
  -e MAX_PAGES=1 \
  ghcr.io/khaidao2/sentiment-pulse-crawler:latest

# 3. inspect
docker exec redpanda rpk topic consume sentiment-pulse.itviec -n 1 -o start
```

Optional UI — Redpanda Console (v3 env names) decodes the Avro to JSON:

```bash
docker run -d --name redpanda-console --network host \
  -e KAFKA_BROKERS=localhost:9092 \
  -e SCHEMAREGISTRY_ENABLED=true \
  -e SCHEMAREGISTRY_URLS=http://localhost:8081 \
  docker.redpanda.com/redpandadata/console:latest
# open http://localhost:8080
```

Cleanup: `docker rm -f redpanda redpanda-console`

## Tests

```bash
PYTHONPATH=data-contracts/src python -m pytest api_crawler/tests -q
```

Tests run offline: a saved detail-page fixture drives the parser, and the parsed
record is validated against the Avro schema (`sentpul.validator`) so any drift
between the parser output and the contract is caught.

## Add a new source

1. Add a `jobs/<name>/` package with `source.py`, `parser.py`, `config.py`,
   `__main__.py` (copy `itviec/` as a template).
2. Reuse `AvroKafkaProducer` and `CrawlService` from `shared/` — only the
   `source`/`parser` are source-specific.
3. Add the matching contract + `.avsc` under `data-contracts/` (see its README).
