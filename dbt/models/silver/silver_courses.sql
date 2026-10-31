-- SILVER / courses: exactly ONE row per course id — the latest crawl.
--
-- The catalogue read that reverse_etl/courses/match.py consumes. Materialized as
-- a TABLE for the same reason silver_current is: it is read repeatedly and the
-- dedup should be precomputed, not recomputed per query.
--
-- Dedup uses ClickHouse's LIMIT ... BY (native, cheap): order rows within each
-- id by crawled_at desc, keep the first. No argMax over the whole history, so
-- RAM stays bounded.
--
-- No content_hash history layer, unlike postings. A course's useful attributes
-- (title, level, duration, licence) barely move, and nobody has asked what a
-- course looked like last quarter — if that changes, this gains a history model
-- beside it rather than growing one in place.
{{ config(
    materialized="table",
    engine="MergeTree",
    order_by="id",
    settings={"allow_nullable_key": 1}
) }}

select *
from {{ ref("bronze_courses") }}
order by id, crawled_at desc
limit 1 by id
