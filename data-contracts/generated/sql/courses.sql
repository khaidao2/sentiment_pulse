CREATE TABLE IF NOT EXISTS courses (
    `id` String,
    `source` String,
    `provider` String,
    `title` String,
    `url` String,
    `summary` Nullable(String),
    `tags` Array(String),
    `language` String,
    `is_free` Bool,
    `level` String,
    `duration_minutes` Nullable(Int32),
    `license_cc` Nullable(Bool),
    `crawled_at` String
) ENGINE = ReplacingMergeTree()
ORDER BY id;
