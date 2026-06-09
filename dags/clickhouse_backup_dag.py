from datetime import datetime, timedelta
from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

_MINIO_ENDPOINT = "http://minio.database.svc.cluster.local:9000"
_MINIO_BUCKET = "clickhouse-backups"
_MINIO_ACCESS_KEY = "minioadmin"
_MINIO_SECRET_KEY = "minioadminpassword"
_CH_HOST = "clickhouse.database.svc.cluster.local"
_CH_PORT = "9000"
_CH_USER = "clickhouse"
_CH_PASSWORD = "clickhousepassword"

default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "start_date": datetime(2026, 1, 1),
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
}

with DAG(
    "clickhouse_backup_dag",
    default_args=default_args,
    description="Daily ClickHouse backup to MinIO",
    schedule="0 2 * * *",
    catchup=False,
    max_active_runs=1,
) as dag:

    ensure_bucket = KubernetesPodOperator(
        task_id="ensure_bucket",
        name="ch-backup-ensure-bucket",
        namespace="airflow",
        image="minio/mc:latest",
        cmds=["/bin/sh", "-c"],
        arguments=[
            f"mc alias set myminio {_MINIO_ENDPOINT} {_MINIO_ACCESS_KEY} {_MINIO_SECRET_KEY}"
            f" && mc mb --ignore-existing myminio/{_MINIO_BUCKET}"
            f" && echo 'Bucket {_MINIO_BUCKET} ready'"
        ],
        in_cluster=True,
        get_logs=True,
        on_finish_action="delete_pod",
    )

    run_backup = KubernetesPodOperator(
        task_id="run_backup",
        name="ch-backup-run",
        namespace="airflow",
        image="clickhouse/clickhouse-client:latest",
        cmds=["/bin/sh", "-c"],
        arguments=[
            f"""
DATE=$(date +%Y-%m-%d) && \
BACKUP_URL="{_MINIO_ENDPOINT}/{_MINIO_BUCKET}/default_$DATE" && \
echo "Backing up ClickHouse default database to $BACKUP_URL ..." && \
clickhouse-client \
    --host {_CH_HOST} \
    --port {_CH_PORT} \
    --user {_CH_USER} \
    --password '{_CH_PASSWORD}' \
    --query "BACKUP DATABASE default TO S3('$BACKUP_URL', '{_MINIO_ACCESS_KEY}', '{_MINIO_SECRET_KEY}')" && \
echo "Backup complete: $BACKUP_URL"
""".strip()
        ],
        in_cluster=True,
        get_logs=True,
        on_finish_action="delete_pod",
    )

    ensure_bucket >> run_backup
