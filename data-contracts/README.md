# sentpul — data contracts

Generate infra, Airflow DAGs and ClickHouse DDL from a single YAML contract per
data source. One contract = one source of truth; `sent-gen` renders the rest so
the same crawl source is never wired by hand in three different places.

## Contract kinds

| Kind          | Nature                                   | Generates                                          |
| ------------- | ---------------------------------------- | -------------------------------------------------- |
| **producer**  | Batch crawl on a schedule; pod exits     | Airflow DAG + ClickHouse DDL                        |
| **streaming** | Long-running consumer/worker (24/7)      | Deployment (+ Service) + ArgoCD App + DDL (if sink) |

`producer` workloads cost ~0 RAM when idle (the pod only lives during the
crawl). `streaming` workloads hold RAM permanently — on the single 16GB node
keep `replicas: 1` and always set `resources.limits`.

## Layout

```
data-contracts/
├── contracts/
│   ├── producer/    # batch crawl contracts (copy default.yaml)
│   └── streaming/   # always-on workload contracts (copy default.yaml)
├── schemas/raw/     # Avro schemas (.avsc)
├── generated/sql/   # generated ClickHouse DDL (DO NOT EDIT)
└── src/sentpul/     # the generator package
```

Generated infra is written to the repo's GitOps tree, which ArgoCD's
app-of-apps already watches:

- `platform-gitops/apps/<name>.yaml` — ArgoCD Application
- `platform-gitops/manifests/<name>.yaml` — Deployment / Service
- `dags/<name>_dag.py` — Airflow DAG

Every generated file carries a `DO NOT EDIT` header — edit the contract, not the
output.

## Producer config → crawler env

A `producer` contract is the single source of truth for its crawler's runtime
config. `sent-gen` turns the contract into env vars on the generated DAG's pod;
the crawler reads only those env vars and never parses the YAML itself (keeps the
crawler image free of `sentpul`/pyyaml and independent of this repo's layout):

- `kafka.topic` → `KAFKA_TOPIC`, `kafka.bootstrap_servers` → `KAFKA_BOOTSTRAP_SERVERS`
- every key in the optional `crawler:` block → the same name upper-cased
  (`listing_url` → `LISTING_URL`, `max_pages` → `MAX_PAGES`, `delay` → `DELAY`)

```yaml
crawler:
  listing_url: https://itviec.com/it-jobs
  max_pages: 5
  delay: 0.5          # seconds between detail fetches (politeness)
```

The crawler itself lives in `api_crawler/` — see its README for the image build
and a local Redpanda end-to-end test.

## Install

```bash
pip install -e .            # from data-contracts/
pip install -e ".[dev]"     # with pytest
```

## Usage

```bash
sent-gen render             # contracts/** -> infra, DAGs, DDL
sent-gen register           # register Avro schemas into Apicurio (idempotent)
sent-gen validate --contract <c.yaml> --data <record.json>
sent-gen run-all            # register + render
```

## Add a new source

1. Drop the Avro schema in `schemas/raw/<name>.avsc`.
2. Copy `contracts/producer/default.yaml` (or `streaming/default.yaml`) to
   `<name>.yaml` and fill in the values.
3. Run `sent-gen render` and review the diff before committing.
