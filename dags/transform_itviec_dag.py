# Hand-written DAG (NOT sent-gen) — runs the dbt medallion transform.
#
# Once a day, AFTER the crawler (crawl_itviec @ 02:00) has produced fresh
# records and the sink has landed them as Parquet in MinIO, this DAG runs
# `dbt build` in a pod using the dbt runner image. dbt reads the bronze Parquet
# via ClickHouse s3(), then builds silver_history -> silver_current (and any
# gold_* models) and runs the data tests. `build` = run + test, so a failing
# test stops the run.
#
# Runs in the `airflow` namespace (same as the crawler pods). MinIO credentials
# come from the `minio-credentials` secret, which must exist in THIS namespace
# (copy it from `database` once). ClickHouse creds are plain (non-secret).
from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from kubernetes.client import models as k8s

default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="transform_itviec",
    default_args=default_args,
    description="dbt build: bronze -> silver (-> gold) for itviec",
    schedule="0 3 * * *",  # 1h after crawl_itviec (0 2 * * *) — data has landed
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
) as dag:
    dbt_build = KubernetesPodOperator(
        task_id="dbt_build",
        name="itviec-dbt-build",
        namespace="airflow",
        image="ghcr.io/khaidao2/sentiment-pulse-dbt:latest",
        image_pull_policy="Always",
        cmds=["dbt"],
        arguments=["build"],
        in_cluster=True,
        get_logs=True,
        on_finish_action="delete_pod",
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "100m", "memory": "256Mi"},
            limits={"cpu": "1000m", "memory": "1Gi"},
        ),
        env_vars=[
            k8s.V1EnvVar(name="CLICKHOUSE_HOST", value="clickhouse.database.svc.cluster.local"),
            k8s.V1EnvVar(name="CLICKHOUSE_PASSWORD", value="clickhousepassword"),
            k8s.V1EnvVar(
                name="MINIO_ACCESS_KEY",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="minio-credentials", key="accessKey")
                ),
            ),
            k8s.V1EnvVar(
                name="MINIO_SECRET_KEY",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="minio-credentials", key="secretKey")
                ),
            ),
        ],
    )
