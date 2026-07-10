-- SILVER / current: exactly ONE row per job_id — the latest crawl.
--
-- This is the "single source of truth, current state" that gold marts and the
-- reverse-ETL DAG read from. Materialized as a TABLE (not a view) because it is
-- read repeatedly and we want the dedup precomputed, not recomputed per query.
--
-- Dedup uses ClickHouse's LIMIT ... BY (native, cheap): order rows within each
-- job_id by crawled_at desc, keep the first. No argMax over the whole history,
-- so RAM stays bounded.
{{ config(
    materialized="table",
    engine="MergeTree",
    order_by="job_id",
    settings={"allow_nullable_key": 1}
) }}

select *
from {{ ref("silver_history") }}
order by job_id, crawled_at desc
limit 1 by job_id
