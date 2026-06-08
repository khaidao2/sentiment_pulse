# vn_stock crawler (`src/`)

Hand-written crawler for the `vn_stock` data source. It fetches equity OHLCV
candles from the [vnstock](https://pypi.org/project/vnstock/) `Market` API,
maps each candle to the `VnStockRecord` Avro schema and produces it to Kafka.

```
vnstock Market.equity(ticker).ohlcv()
        │  (mapper.to_record)
        ▼
VnStockRecord dict ──► VnStockProducer.send() ──► Kafka topic
                                                  sentiment-pulse.vn_stock
                                                        │  (generated sink)
                                                        ▼
                                                  ClickHouse `vn_stock`
```

## Layout

| File          | Role                                                            |
|---------------|-----------------------------------------------------------------|
| `config.py`   | `CrawlerConfig` — tickers, interval, date window (env-driven).  |
| `mapper.py`   | `to_record(ticker, row)` — OHLCV row → `VnStockRecord` dict.    |
| `crawler.py`  | `VnStockCrawler` — fetch via vnstock, map, push to a producer.  |
| `__main__.py` | CLI: loads the generated `VnStockProducer` and runs the crawl.  |

The producer/sink/DAG that surround this package are **auto-generated** by
`sent-gen` from `data-contracts/infra/vn_stock.yaml`. The crawl logic is *not*
generated — it lives here.

## Run

The producer must exist first (it is generated, not committed):

```bash
sent-gen render                 # creates ../producer.py, ../sink.py, dags/vn_stock_dag.py
python -m api_crawler.vn_stock.src   # run from the repo root
```

Config via environment variables:

| Var                       | Default                  | Meaning                          |
|---------------------------|--------------------------|----------------------------------|
| `VN_STOCK_TICKERS`        | `FPT,VCB,HPG,VNM,VIC`    | Comma-separated tickers (ignored when `VN_STOCK_CRAWL_ALL` is on). |
| `VN_STOCK_CRAWL_ALL`      | `false`                  | When `true`/`1`/`yes`, crawl every symbol on the market via vnstock `Listing().all_symbols()` instead of `VN_STOCK_TICKERS`. |
| `VN_STOCK_INTERVAL`       | `1D`                     | `1m,5m,15m,30m,1h,1D,1W`.        |
| `VN_STOCK_LOOKBACK_DAYS`  | `7`                      | Rolling window when no start/end.|
| `VN_STOCK_START` / `_END` | *(unset)*                | Explicit `YYYY-MM-DD` window.    |
| `KAFKA_BOOTSTRAP_SERVERS` | *(producer default)*     | Override Kafka brokers.          |

## Wiring into Airflow

The generated `dags/vn_stock_dag.py` ships with a placeholder `crawl_*` task.
To run a real crawl, point that task at this package:

```python
from api_crawler.vn_stock.src.crawler import VnStockCrawler
from api_crawler.vn_stock.producer import VnStockProducer

def crawl_vn_stock_task():
    VnStockCrawler(producer=VnStockProducer()).crawl()
```
