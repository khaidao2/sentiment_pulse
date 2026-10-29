-- SILVER / history: cleaned, typed, ONE row per (job, distinct content version).
--
-- Keeps history but NOT every daily re-crawl: a content_hash collapses identical
-- snapshots so an unchanged posting re-crawled 100 times stays ~1 row, while a
-- real change (new salary, edited description, ...) lands as a new row.
--
-- RAM discipline (ClickHouse is capped at 2Gi):
--   * incremental  -> each run only reads/inserts rows newer than what we have,
--                     not the whole history.
--   * ReplacingMergeTree ORDER BY (job_id, content_hash) -> the engine dedups
--                     identical (job, content) at MERGE time, streaming, bounded.
{{ config(
    materialized="incremental",
    engine="ReplacingMergeTree",
    order_by="(job_id, content_hash)",
    incremental_strategy="append",
    settings={"allow_nullable_key": 1}
) }}

select
    -- job_id is Nullable(String) in bronze; we filter out nulls below, so make it
    -- a clean non-null String — a sorting key can't be Nullable without a flag.
    * replace (assumeNotNull(job_id) as job_id),
    -- "did the posting change?" fingerprint over the meaningful content fields.
    -- Extend this list if you want more fields to count as a change.
    cityHash64(
        title,
        salary_type,
        coalesce(salary_min_usd, 0),
        coalesce(salary_max_usd, 0),
        location,
        work_arrangement,
        coalesce(description, ''),
        arrayStringConcat(tech_stack, '|'),
        arrayStringConcat(skills, '|'),
        arrayStringConcat(responsibilities, '|'),
        arrayStringConcat(benefits, '|')
    ) as content_hash
from {{ ref("bronze_all_sources") }}
where job_id is not null

{% if is_incremental() %}
    -- only the new slice, with a 1-day look-back: a batch the sink flushes
    -- after this model last ran can carry crawled_at older than the current
    -- max, and a strict `>` would skip it forever. Re-read overlap rows have the
    -- same (job_id, content_hash) and ReplacingMergeTree collapses them.
    and parseDateTimeBestEffortOrNull(crawled_at) >
        (select parseDateTimeBestEffort(max(crawled_at)) - interval 1 day from {{ this }})
{% endif %}
