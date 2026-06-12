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