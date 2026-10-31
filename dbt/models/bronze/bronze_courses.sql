-- BRONZE: raw course records exactly as the sink landed them in MinIO
-- (date-partitioned Parquet). A VIEW, like the posting sources: dbt copies
-- nothing, ClickHouse reads the Parquet in place through s3() on every query.
--
-- Raw layer = as-is: keeps duplicates. The courses crawl is a full re-read of
-- every catalogue each week, so a course that stays in a catalogue lands again
-- on every run. Dedup happens in silver_courses, not here.
--
-- The structure is DECLARED, not inferred, which is the one difference from
-- bronze_itviec. `dbt build` takes no selector, so this model joins the nightly
-- transform the moment it merges — and schema inference over a glob that
-- matches no files cannot determine a structure, so an empty courses/ prefix
-- would fail the whole medallion build, for every source, until the first crawl
-- lands. Declaring the columns makes the view return zero rows instead. It also
-- pins column order, which is the hazard bronze_all_sources documents.
--
-- These types mirror the Avro schema through the sink's own mapping
-- (api_crawler/shared/adapters/avro_arrow.py): enum -> String, ["null", X] ->
-- Nullable(X), array -> Array. They are the same columns the generated DDL
-- carries, so data-contracts/schemas/learning/courses.avsc stays the one source
-- of truth — if a field is added there, add it here too.
--
-- NOT part of bronze_all_sources: that brace glob unions the three job sources,
-- which share one field-for-field identical schema. Courses have a different
-- schema entirely, and a positional union across the two would line up
-- same-typed columns that mean different things.
{{ config(materialized="view") }}

select *
from s3(
    '{{ env_var("BRONZE_S3_URL_COURSES", "http://minio.database.svc.cluster.local:9000/sentiment-pulse-raw/courses/*/*.parquet") }}',
    '{{ env_var("MINIO_ACCESS_KEY") }}',
    '{{ env_var("MINIO_SECRET_KEY") }}',
    'Parquet',
    'id String,
     source String,
     provider String,
     title String,
     url String,
     summary Nullable(String),
     tags Array(String),
     language String,
     is_free Bool,
     level String,
     duration_minutes Nullable(Int32),
     license_cc Nullable(Bool),
     crawled_at String'
)
