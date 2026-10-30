# Hand-written DAG (NOT sent-gen) — reverse ETL: silver_current -> web Postgres.
#
# Runs after transform_itviec (03:00) has refreshed silver_current, and pushes
# the changed slice into the web app's Postgres `jobs` table via an idempotent
# upsert. Data activation — the web reads its own Postgres; this only writes.
#
# Runs in the `airflow` namespace. Postgres connection comes from the
# `webapp-postgres-credentials` secret (key `dsn`), created when the web's
# Postgres is deployed. The ClickHouse password comes from the
# `clickhouse-credentials` secret (key `password`) — see transform_itviec_dag.py.
from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from kubernetes.client import models as k8s

default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="reverse_etl_itviec",
    default_args=default_args,
    description="Upsert silver_current -> web Postgres jobs table",
    schedule="0 4 * * *",  # after transform_itviec (0 3 * * *)
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
) as dag:
    sync = KubernetesPodOperator(
        task_id="sync_jobs",
        name="itviec-reverse-etl",
        namespace="airflow",
        image="ghcr.io/khaidao2/sentiment-pulse-reverse-etl:latest",
        image_pull_policy="Always",
        cmds=["python"],
        arguments=["sync_jobs.py"],
        in_cluster=True,
        get_logs=True,
        on_finish_action="delete_pod",
        # A cold image pull on a node that has never seen this image routinely
        # exceeds KPO's 120s default and fails the task before the container
        # starts (seen four times on 2026-10-29/30: dbt_build, sync_jobs and two
        # crawls, each succeeding on the retry once the layers were local). The
        # pull is not the problem; the default budget is.
        startup_timeout_seconds=600,
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": "128Mi"},
            limits={"cpu": "500m", "memory": "512Mi"},
        ),
        env_vars=[
            k8s.V1EnvVar(name="CLICKHOUSE_HOST", value="clickhouse.database.svc.cluster.local"),
            k8s.V1EnvVar(
                name="CLICKHOUSE_PASSWORD",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="clickhouse-credentials", key="password")
                ),
            ),
            k8s.V1EnvVar(
                name="POSTGRES_DSN",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="webapp-postgres-credentials", key="dsn")
                ),
            ),
        ],
    )
