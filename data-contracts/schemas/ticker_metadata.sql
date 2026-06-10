-- Reference table for ticker metadata, used as the source for the
-- `ticker_dict` Dictionary (so dashboards/queries can `dictGet('ticker_dict',
-- 'organ_name', ticker)` instead of joining a large fact table).
CREATE TABLE IF NOT EXISTS ticker_metadata (
    `ticker` String,
    `organ_name` String,
    `exchange` String,
    `industry` String,
    `updated_at` DateTime64(6)
) ENGINE = ReplacingMergeTree()
ORDER BY ticker;

-- SOURCE needs explicit credentials: the ClickHouse dictionary source connects
-- as user `default` (no password) otherwise, which fails on this cluster.
CREATE DICTIONARY IF NOT EXISTS ticker_dict (
    `ticker` String,
    `organ_name` String,
    `exchange` String,
    `industry` String
)
PRIMARY KEY ticker
SOURCE(CLICKHOUSE(TABLE 'ticker_metadata' DB 'default' USER 'clickhouse' PASSWORD 'clickhousepassword'))
LAYOUT(HASHED())
LIFETIME(MIN 300 MAX 600);

-- Seed data for the default ticker basket (api_crawler/vn_stock/src/config.py
-- DEFAULT_TICKERS). Add a row here whenever a new ticker is added to the basket.
INSERT INTO ticker_metadata (ticker, organ_name, exchange, industry, updated_at) VALUES
    ('FPT', 'Cong ty Co phan FPT', 'HOSE', 'Cong nghe thong tin', now64(6)),
    ('VCB', 'Ngan hang TMCP Ngoai thuong Viet Nam', 'HOSE', 'Ngan hang', now64(6)),
    ('HPG', 'Tap doan Hoa Phat', 'HOSE', 'Thep', now64(6)),
    ('VNM', 'Cong ty Co phan Sua Viet Nam', 'HOSE', 'Thuc pham va Do uong', now64(6)),
    ('VIC', 'Tap doan Vingroup', 'HOSE', 'Bat dong san', now64(6));
