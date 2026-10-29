-- BRONZE: raw careerviet postings exactly as the sink landed them in MinIO
-- (date-partitioned Parquet). A VIEW — ClickHouse reads the Parquet in place via
-- s3() on every query, server-side, so this works even with dbt on your laptop.
--
-- Raw layer = as-is: keeps duplicates (at-least-once + retries). Dedup happens
-- in silver, not here. Kept per-source so a single source can be inspected and
-- tested on its own; silver reads bronze_all_sources instead (see that model).
{{ config(materialized="view") }}

select *
from s3(
    '{{ env_var("BRONZE_S3_URL_CAREERVIET", "http://minio.database.svc.cluster.local:9000/sentiment-pulse-raw/careerviet/*/*.parquet") }}',
    '{{ env_var("MINIO_ACCESS_KEY") }}',
    '{{ env_var("MINIO_SECRET_KEY") }}',
    'Parquet'
)
settings schema_inference_mode = 'union'
