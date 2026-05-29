import os
import requests

DEFAULT_APICURIO_URL = "http://apicurio.kafka.svc.cluster.local:80"

def get_apicurio_url() -> str:
    """Returns the Apicurio Registry API endpoint."""
    return os.environ.get("APICURIO_REGISTRY_URL", DEFAULT_APICURIO_URL).rstrip("/")

def register_schema(group_id: str, artifact_id: str, schema: dict) -> bool:
    """Registers or updates an Avro schema in Apicurio Registry."""
    base_url = get_apicurio_url()
    url = f"{base_url}/apis/registry/v2/groups/{group_id}/artifacts"
    
    headers = {
        "Content-Type": "application/json",
        "X-Registry-ArtifactId": artifact_id,
        "X-Registry-ArtifactType": "AVRO"
    }
    
    try:
        response = requests.post(url, headers=headers, json=schema, timeout=10)
        if response.status_code in [200, 201]:
            print(f"Successfully registered schema '{artifact_id}' in group '{group_id}'.")
            return True
        elif response.status_code == 409:
            # Artifact already exists, update (create new version)
            update_url = f"{base_url}/apis/registry/v2/groups/{group_id}/artifacts/{artifact_id}"
            update_headers = {"Content-Type": "application/json"}
            update_resp = requests.put(update_url, headers=update_headers, json=schema, timeout=10)
            if update_resp.status_code in [200, 201]:
                print(f"Successfully updated version for schema '{artifact_id}' in group '{group_id}'.")
                return True
            print(f"Failed to update schema '{artifact_id}': {update_resp.status_code} - {update_resp.text}")
            return False
        else:
            print(f"Failed to register schema '{artifact_id}': {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"Error connecting to Apicurio Registry ({base_url}): {e}")
        return False
