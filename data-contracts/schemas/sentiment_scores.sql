CREATE TABLE IF NOT EXISTS sentiment_scores (
    `id` String,
    `event_time` DateTime64(6),
    `source` String,
    `ticker` String,
    `score` Float32,
    `label` String,
    `intensity` Float32,
    `headline` String,
    `url` Nullable(String),
    `created_at` DateTime64(6)
) ENGINE = ReplacingMergeTree()
ORDER BY id;