-- BRONZE: raw itviec postings exactly as the sink landed them in MinIO
-- (date-partitioned Parquet). This is a VIEW — dbt copies nothing; ClickHouse
-- reads the Parquet in place, server-side, via the s3() table function on every
-- query. The s3() call runs ON the ClickHouse server, so this works even when
-- dbt is on your laptop (only ClickHouse needs to reach MinIO).
--
-- Raw layer = as-is: keeps duplicates (at-least-once + retries). Dedup happens
-- in silver, not here.
{{ config(materialized="view") }}

select *
from s3(
    '{{ env_var("BRONZE_S3_URL", "http://minio.database.svc.cluster.local:9000/sentiment-pulse-raw/itviec/*/*.parquet") }}',
    '{{ env_var("MINIO_ACCESS_KEY") }}',
    '{{ env_var("MINIO_SECRET_KEY") }}',
    'Parquet'
)
