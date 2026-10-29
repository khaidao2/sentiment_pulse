-- BRONZE: every source in ONE s3() read, via a brace glob over the sink prefixes.
--
-- Why one read instead of `union all` over the per-source views: ClickHouse
-- matches UNION ALL columns BY POSITION. The three Avro schemas are field-for-
-- field identical today, but each bronze view infers its own column order from
-- whatever Parquet files that prefix happens to hold, and `schema_inference_mode
-- = 'union'` can order a prefix's columns differently once one source has files
-- predating a column the others already have. A positional union would then line
-- up same-typed columns that mean different things -- silent corruption, exactly
-- what the salary_*_usd unit mismatch would have caused. A single s3() call runs
-- ONE inference pass across all files, so every column lands by name.
--
-- Add a source by adding its prefix to the brace list.
{{ config(materialized="view") }}

select *
from s3(
    '{{ env_var("BRONZE_S3_URL_ALL", "http://minio.database.svc.cluster.local:9000/sentiment-pulse-raw/{itviec,topcv,careerviet}/*/*.parquet") }}',
    '{{ env_var("MINIO_ACCESS_KEY") }}',
    '{{ env_var("MINIO_SECRET_KEY") }}',
    'Parquet'
)
settings schema_inference_mode = 'union'
