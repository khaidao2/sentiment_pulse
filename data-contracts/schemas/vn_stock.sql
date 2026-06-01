CREATE TABLE IF NOT EXISTS vn_stock (
    `id` String,
    `ticker` String,
    `open_price` Float32,
    `close_price` Float32,
    `high_price` Float32,
    `low_price` Float32,
    `volume` Int64,
    `trading_date` String,
    `created_at` String
) ENGINE = ReplacingMergeTree()
ORDER BY id;