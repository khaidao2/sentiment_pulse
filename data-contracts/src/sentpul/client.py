"""HTTP clients for external services.

`BaseClient` holds the shared plumbing (base URL, a reused session, a default
timeout, consistent URL joining). Concrete clients subclass it and add the
service-specific calls. Following the "library raises, CLI prints" rule, these
raise on failure rather than returning a bool.
"""

from __future__ import annotations

import json
import os

import requests


class BaseClient:
    """Minimal HTTP client: base URL + a shared session and default timeout."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, self._url(path), **kwargs)

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self.request("PUT", path, **kwargs)


# Apicurio's Confluent-compatible Schema Registry API — the SAME endpoint and
# id-space the crawler's Avro producer uses at runtime. Registering here and in
# the producer therefore hits one subject / one schema id (no divergent
# identities). Override with SCHEMA_REGISTRY_URL (shared with the crawler).
DEFAULT_REGISTRY_URL = "http://apicurio.kafka.svc.cluster.local:80/apis/ccompat/v6"


class ApicurioClient(BaseClient):
    """Confluent-compatible Schema Registry client (Apicurio /apis/ccompat/v6)."""

    def __init__(self, base_url: str | None = None, timeout: float = 10.0) -> None:
        base_url = base_url or os.environ.get("SCHEMA_REGISTRY_URL", DEFAULT_REGISTRY_URL)
        super().__init__(base_url, timeout)

    def register_schema(self, subject: str, schema: dict) -> int:
        """Register an Avro schema under `subject` and return its integer id.

        Idempotent: if the schema is already registered the registry returns the
        existing id. Convention: subject = "<topic>-value". `schema` must be the
        RAW schema dict (json.load of the .avsc file) — NOT a fastavro-parsed one
        — so the serialized string matches what the producer registers.
        """
        payload = {"schema": json.dumps(schema), "schemaType": "AVRO"}
        headers = {"Content-Type": "application/vnd.schemaregistry.v1+json"}

        resp = self.post(f"/subjects/{subject}/versions", json=payload, headers=headers)
        if resp.status_code in (200, 201):
            return resp.json()["id"]

        # Already present — look up the existing id under the subject.
        resp = self.post(f"/subjects/{subject}", json=payload, headers=headers)
        if resp.status_code in (200, 201):
            return resp.json()["id"]

        raise RuntimeError(
            f"schema registry register failed for subject '{subject}': "
            f"{resp.status_code} {resp.text}"
        )
