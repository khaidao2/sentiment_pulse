import os

files = [
    "data-platform-realm.json",
    "platform-gitops/manifests/keycloak-ingress.yaml",
    "platform-gitops/manifests/argocd-ingress.yaml",
    "platform-gitops/manifests/kafka-ui.yaml",
    "platform-gitops/helm-values/grafana-values.yaml",
    "platform-gitops/helm-values/oauth2-proxy-values.yaml",
    "platform-gitops/helm-values/airflow-values.yaml",
    "platform-gitops/manifests/cloudflared.yaml",
    "README.md"
]

for f in files:
    path = os.path.join("/home/thekhai/code/sentiment_pulse", f)
    with open(path, "r") as file:
        content = file.read()
    
    # Domains
    content = content.replace("airflow.local", "airflow.sentpul.click")
    content = content.replace("auth.local", "auth.sentpul.click")
    content = content.replace("grafana.local", "grafana.sentpul.click")
    content = content.replace("argocd.local", "argocd.sentpul.click")
    content = content.replace("kafka-ui.local", "kafka-ui.sentpul.click")
    content = content.replace("minio.local", "minio.sentpul.click")
    content = content.replace("minio-api.local", "minio-api.sentpul.click")
    content = content.replace("clickhouse.local", "clickhouse.sentpul.click")

    # Force HTTPS for redirect and web origin URLs because they are external
    content = content.replace("http://airflow.sentpul.click", "https://airflow.sentpul.click")
    content = content.replace("http://grafana.sentpul.click", "https://grafana.sentpul.click")
    content = content.replace("http://argocd.sentpul.click", "https://argocd.sentpul.click")
    content = content.replace("http://kafka-ui.sentpul.click", "https://kafka-ui.sentpul.click")
    content = content.replace("http://minio.sentpul.click", "https://minio.sentpul.click")
    content = content.replace("http://minio-api.sentpul.click", "https://minio-api.sentpul.click")
    content = content.replace("http://clickhouse.sentpul.click", "https://clickhouse.sentpul.click")

    # Note: I will leave http://auth.sentpul.click as HTTP because it's used internally
    # and we don't have SSL configured on the internal Traefik ingress yet.

    with open(path, "w") as file:
        file.write(content)
print("Done")
