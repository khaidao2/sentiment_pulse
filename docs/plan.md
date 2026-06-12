# SentimentPulse — Product & Technical Plan

## 1. Tổng quan sản phẩm

SentimentPulse là hệ thống **Market Intelligence Dashboard** sử dụng AI và NLP để phân tích tâm lý thị trường tài chính theo thời gian thực từ nhiều nguồn dữ liệu (báo chí, mạng xã hội, diễn đàn đầu tư).

Điểm cốt lõi: **Phát hiện phân kỳ tâm lý (Sentiment Divergence)** — đối chiếu liên tục giữa cảm xúc cộng đồng và hành vi dòng tiền thực tế để phát hiện sớm FOMO, bull trap, panic bất thường, tích lũy ẩn.

### Tín hiệu đặc trưng

| Tên tín hiệu | Điều kiện |
|---|---|
| **Euphoria Trap** | Sentiment cộng đồng tăng mạnh + khối ngoại bán ròng + thanh khoản suy yếu |
| **Hidden Accumulation** | Tin xấu lan rộng + dòng tiền lớn mua vào âm thầm |
| **Panic Spike** | Sentiment tiêu cực bùng nổ + giá hầu như không giảm |
| **FOMO Overload** | Mức hưng phấn vượt ngưỡng lịch sử + volume tăng đột biến không bền vững |

---

## 2. Đối tượng người dùng

- **Nhà đầu tư cá nhân**: theo dõi tâm lý thị trường, giảm FOMO/panic
- **Analyst công ty chứng khoán**: rút ngắn thời gian tổng hợp thông tin, hỗ trợ viết báo cáo
- **Bộ phận quản trị rủi ro**: phát hiện bất thường sớm, phản ứng trước khi thị trường biến động mạnh
- **Ngân hàng / fund**: AI-powered market intelligence, monitoring realtime

---

## 3. Kiến trúc hệ thống

```
┌─────────────────────────────────────────────────────────┐
│                     Data Sources                        │
│  CafeF · VnExpress · Reddit · Facebook Groups          │
│  Báo cáo tài chính · Diễn đàn đầu tư                  │
│  vnstock · SSI Fast Connect · Market Feed              │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│               Ingestion Layer                           │
│        Apache Kafka (streaming pipeline)               │
│   Topic: raw-news · raw-social · raw-market-data       │
└────────────────────┬────────────────────────────────────┘
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
┌──────────────────┐  ┌─────────────────────────────────┐
│   NLP Pipeline   │  │      Market Data Pipeline       │
│                  │  │                                 │
│  PhoBERT (VI)    │  │  Giá · Thanh khoản · Volume    │
│  FinBERT (EN)    │  │  Biến động intraday             │
│                  │  │  Giao dịch khối ngoại/tổ chức   │
│  → sentiment     │  │                                 │
│  → emotion score │  │  Spark Structured Streaming     │
│  → ticker tags   │  │                                 │
└────────┬─────────┘  └──────────────┬──────────────────┘
         │                           │
         └──────────┬────────────────┘
                    ▼
┌─────────────────────────────────────────────────────────┐
│              Divergence Engine (Core)                   │
│                                                         │
│  Prophet · LSTM · Anomaly Detection                    │
│                                                         │
│  → Divergence Score (sentiment vs. money flow)         │
│  → Realtime Alerts                                     │
│  → Risk Heatmap                                        │
│  → LLM-generated analyst summary (tiếng Việt)         │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                  Storage Layer                          │
│  ClickHouse (OLAP · timeseries)                        │
│  Kafka Topics (realtime sink)                          │
│  MinIO (raw archive)                                   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                 Presentation Layer                      │
│  Grafana Dashboard · React SPA · FastAPI               │
│                                                         │
│  - Sentiment per ticker / sector                       │
│  - FOMO/Panic index                                    │
│  - Money flow by sector                                │
│  - Divergence alerts realtime                          │
│  - AI-generated daily report                           │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Các thành phần kỹ thuật

### 4.1 Data Collection

| Nguồn | Loại | Phương thức | Ưu tiên |
|---|---|---|---|
| CafeF | Báo chí tài chính | Crawler (Playwright/BS4) | P0 |
| VnExpress | Báo chí | Crawler (curl-cffi/BS4) | P0 |
| vnstock | OHLCV + khối ngoại | API (đã có) | P0 |
| chungsy.vn | Cộng đồng đầu tư | API Crawler | P1 |
| Reddit (r/VietnamInvesting) | Mạng xã hội | Reddit API | P1 |
| Facebook Groups | Mạng xã hội | Graph API / Crawler | P2 |
| Diễn đàn đầu tư (F319, CafeF forum) | Diễn đàn | Crawler | P1 |
| SSI Fast Connect | Realtime market feed | WebSocket | P2 |

### 4.2 NLP Pipeline

- **PhoBERT**: sentiment, emotion cho văn bản tiếng Việt
- **FinBERT**: tài liệu tiếng Anh, báo cáo tài chính
- Output: `sentiment_score [-1, 1]`, `emotion_label`, `intensity`, `ticker_mentions[]`
- Batch inference qua Spark / online inference qua FastAPI + model serving

### 4.3 Market Data Pipeline

- **Nguồn hiện tại**: vnstock (OHLCV daily, đã crawl)
- **Cần thêm**: khối ngoại intraday, volume profile, institutional flow
- Processing: Spark Structured Streaming → ClickHouse

### 4.4 Divergence Engine

```
divergence_score = f(
    sentiment_velocity,   # tốc độ thay đổi sentiment
    price_velocity,       # tốc độ thay đổi giá
    volume_delta,         # bất thường về volume
    foreign_flow,         # hướng dòng tiền khối ngoại
    anomaly_z_score       # độ lệch so với baseline lịch sử
)
```

- **Prophet**: dự báo baseline sentiment theo mùa/tuần/tháng
- **LSTM**: học pattern divergence từ lịch sử
- **Anomaly Detection**: Isolation Forest / Z-score cho realtime alerting

### 4.5 LLM Report Generation

- Input: divergence signals + news snippets + market summary
- Output: báo cáo tiếng Việt dạng analyst report (~300-500 từ)
- Model: Claude API hoặc fine-tuned VinaLLaMA
- Trigger: mỗi EOD hoặc khi divergence score vượt ngưỡng

### 4.6 Dashboard

- **Grafana**: metrics, timeseries, alerting (đã có, tích hợp ClickHouse)
- **React + FastAPI**: custom UI cho divergence view, heatmap, alert feed
- **Plotly Dash** (optional): nếu cần interactive exploration nhanh

---

## 5. Roadmap

### Phase 1 — Foundation (đang làm)
- [x] OHLCV crawler (vnstock) + Kafka pipeline
- [x] ClickHouse storage + Grafana dashboard
- [x] Airflow orchestration + remote logging
- [x] **VnExpress news crawler (RSS & HTML)**
- [x] **chungsy.vn community crawler (JSON API)**
- [x] **Kafka topics `sentiment-pulse.news` & `sentiment-pulse.community` + Apicurio schema registry**

### Phase 2 — NLP Core
- [ ] PhoBERT inference service (FastAPI + Docker)
- [ ] Sentiment sink: Kafka → ClickHouse table `sentiment_scores`
- [ ] Per-ticker sentiment aggregation (5m, 1h, 1D windows)
- [ ] Airflow DAG: daily news crawl → NLP → ClickHouse

### Phase 3 — Divergence Engine
- [ ] ClickHouse view: join `sentiment_scores` + `vn_stock_ohlcv`
- [ ] Divergence score computation (rule-based MVP trước)
- [ ] Anomaly detection pipeline
- [ ] Realtime alert → Kafka topic `divergence-alerts`

### Phase 4 — Dashboard & Reporting
- [ ] Grafana panels: sentiment per ticker, divergence heatmap
- [ ] FastAPI `/alerts` endpoint
- [ ] LLM daily report generation (Claude API)
- [ ] React frontend (nếu cần UI custom)

### Phase 5 — Scale & Enrichment
- [ ] Reddit / forum crawler
- [ ] SSI Fast Connect (intraday data)
- [ ] LSTM model training trên historical divergence
- [ ] Multi-tenant / SaaS packaging

---

## 6. Schema dữ liệu chính

### `sentiment_scores` (ClickHouse)
```sql
CREATE TABLE sentiment_scores (
    event_time   DateTime,
    source       LowCardinality(String),   -- cafef, vnexpress, reddit, ...
    ticker       LowCardinality(String),   -- VNM, FPT, VIC, ... hoặc INDEX
    score        Float32,                  -- [-1.0, 1.0]
    label        LowCardinality(String),   -- positive, negative, neutral
    intensity    Float32,                  -- [0.0, 1.0]
    headline     String,
    url          String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_time)
ORDER BY (ticker, event_time);
```

### `divergence_signals` (ClickHouse)
```sql
CREATE TABLE divergence_signals (
    signal_time       DateTime,
    ticker            LowCardinality(String),
    signal_type       LowCardinality(String),  -- euphoria_trap, hidden_accum, panic_spike, fomo
    divergence_score  Float32,
    sentiment_avg_1h  Float32,
    price_change_1h   Float32,
    volume_ratio      Float32,
    foreign_net_flow  Float64,
    severity          LowCardinality(String)   -- low, medium, high, critical
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(signal_time)
ORDER BY (ticker, signal_time);
```

### `news` (ClickHouse)
```sql
CREATE TABLE IF NOT EXISTS news (
    `id` String,
    `title` String,
    `content` String,
    `author` Nullable(String),
    `source` String,
    `url` Nullable(String),
    `published_at` String,
    `created_at` String
) ENGINE = ReplacingMergeTree()
ORDER BY id;
```

### `community` (ClickHouse)
```sql
CREATE TABLE IF NOT EXISTS community (
    `id` String,
    `title` String,
    `content` String,
    `author` Nullable(String),
    `source` String,
    `url` Nullable(String),
    `published_at` String,
    `created_at` String
) ENGINE = ReplacingMergeTree()
ORDER BY id;
```

---

## 7. Dependencies & Infrastructure hiện có

| Component | Status | Notes |
|---|---|---|
| Apache Kafka + Apicurio | Running | Schema registry tích hợp |
| ClickHouse | Running | OHLCV data đã có |
| Airflow 3.x | Running | KubernetesExecutor, remote logs MinIO |
| MinIO | Running | Backup + log storage |
| Grafana | Running | ClickHouse datasource + Keycloak SSO |
| Keycloak | Running | SSO cho toàn platform |
| K3s (K8s) | Running | Single-node, Tailscale access |

---

## 8. Rủi ro & Giải pháp

| Rủi ro | Mức độ | Giải pháp |
|---|---|---|
| vnstock rate limit (60 req/min) | Cao | `time.sleep(1.1)` giữa các ticker (đã fix) |
| Facebook crawl bị block | Cao | Ưu tiên public forum (F319, CafeF forum) thay thế |
| PhoBERT latency cao | Trung bình | Batch inference + cache; chạy offline với Airflow DAG |
| Divergence false positives | Trung bình | Rule-based filter + ngưỡng lịch sử; human review ban đầu |
| SSI API không ổn định | Thấp | Fallback về vnstock cho intraday |
