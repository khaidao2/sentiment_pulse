"""Shared infrastructure config — read once from the environment.

These are cluster-wide endpoints shared by every source. Per-source values
(topic, schema_path, subject) are passed into the adapter constructor instead.
"""

import os

KAFKA_BOOTSTRAP_SERVERS = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS", "kafka.kafka.svc.cluster.local:9092"
)

# Apicurio's Confluent-compatible Schema Registry API (/apis/ccompat/v6).
SCHEMA_REGISTRY_URL = os.environ.get(
    "SCHEMA_REGISTRY_URL",
    "http://apicurio.kafka.svc.cluster.local:80/apis/ccompat/v6",
)
