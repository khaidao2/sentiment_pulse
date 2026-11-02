# Hand-written DAG (NOT sent-gen) — reverse ETL: silver_courses -> web Postgres.
#
# The courses half of the activation path, in the order the data depends on:
#
#   crawl_courses       Sun 01:00  (sent-gen, courses_dag.py)  -> Kafka -> MinIO
#   transform_itviec    daily 03:00  runs a bare `dbt build`, which has no
#                       selector and therefore builds bronze_courses and
#                       silver_courses along with everything else
#   THIS                Sun 05:00  silver_courses -> courses, course_hierarchy,
#                                  then course_skills
#
# Sunday, because the crawl is weekly and running this daily would re-derive an
# unchanged catalogue six times for nothing. 05:00 leaves the 03:00 dbt build
# and the 04:00 reverse_etl_itviec run clear.
#
# TASK ORDER IS A DATA DEPENDENCY, not tidiness. sync_course_skills writes a
# junction whose course_id must already exist in `courses`; it skips and counts
# any course it cannot find rather than inventing one, so running it first would
# quietly produce zero edges on a cold target.
#
# Runs in the `airflow` namespace. Postgres comes from the
# `webapp-postgres-credentials` secret (key `dsn`) and ClickHouse from
# `clickhouse-credentials` (key `password`) — the same two secrets
# reverse_etl_itviec_dag.py uses; see transform_itviec_dag.py for how they were
# created.
#
# NOT ENABLED BY DEFAULT: sync_course_skills writes `course_skills`, a table the
# web app has not migrated yet (its 20261102090000_add_courses_catalogue.sql
# deliberately left it out). The task fails loudly against a database without
# it, which is why this DAG ships paused — unpause it once the web has migrated.
from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from kubernetes.client import models as k8s

IMAGE = "ghcr.io/khaidao2/sentiment-pulse-reverse-etl:latest"

default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

ENV = [
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
]

RESOURCES = k8s.V1ResourceRequirements(
    requests={"cpu": "50m", "memory": "128Mi"},
    limits={"cpu": "500m", "memory": "512Mi"},
)


def _task(task_id: str, name: str, arguments: list[str]) -> KubernetesPodOperator:
    return KubernetesPodOperator(
        task_id=task_id,
        name=name,
        namespace="airflow",
        image=IMAGE,
        image_pull_policy="Always",
        cmds=["python"],
        arguments=arguments,
        in_cluster=True,
        get_logs=True,
        on_finish_action="delete_pod",
        # A cold image pull on a node that has never seen this image routinely
        # exceeds KPO's 120s default and fails the task before the container
        # starts (seen four times on 2026-10-29/30: dbt_build, sync_jobs and two
        # crawls, each succeeding on the retry once the layers were local). The
        # pull is not the problem; the default budget is.
        startup_timeout_seconds=600,
        container_resources=RESOURCES,
        env_vars=ENV,
    )


with DAG(
    dag_id="reverse_etl_courses",
    default_args=default_args,
    description="Upsert silver_courses -> web Postgres courses, hierarchy and skills",
    schedule="0 5 * * 0",  # Sunday, after the weekly crawl and the 03:00 dbt build
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,  # see the note at the top of this file
    tags=["courses", "reverse-etl"],
) as dag:
    sync_courses = _task(
        "sync_courses", "courses-reverse-etl", ["sync_courses.py"],
    )
    # --apply, because the script's default is a dry run on purpose: it rewrites
    # a junction derived from a dictionary, and a human running it by hand
    # should see the plan before the write. Airflow is not that human.
    sync_course_skills = _task(
        "sync_course_skills", "courses-skills-reverse-etl",
        ["sync_course_skills.py", "--apply"],
    )

    sync_courses >> sync_course_skills
