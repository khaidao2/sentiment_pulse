"""Every secret a DAG reads must be sealed into the namespace its pods run in.

THIS BUG HAS NOW HAPPENED TWICE, both times the same shape: a DAG was moved to
a `secretKeyRef` and nothing created the secret on the other side.

  * clickhouse-credentials -- commit 67a26ec moved the password out of the
    DAGs into `secretKeyRef: clickhouse-credentials/password` and added no
    secret. It existed in NO namespace, and transform_itviec and
    reverse_etl_itviec failed every run.
  * ai-gateway-credentials -- reverse_etl_itviec's match_jobs_ai reads
    endpoint/api_key/model, but the only copy was sealed to the WEBAPP
    namespace while the task pod runs in airflow.

BOTH FAILED THE SAME UNREADABLE WAY. A missing secret is not a crash with a
traceback: the pod cannot be CREATED, so KubernetesPodOperator sits in
CreateContainerConfigError until `startup_timeout_seconds` expires -- ten
minutes here -- and reports a timeout. Nothing in the message says "secret",
the duration looks like slow work, and the only place the real reason appears
is a Kubernetes event on a pod that `on_finish_action="delete_pod"` has already
removed.

So the cheap check is worth a great deal: it is a set comparison over two
directories, it needs no cluster, and it turns a silent ten-minute daily
failure into a named test.

WHAT IT DOES NOT CHECK: that the sealed VALUES are correct, or that the key
names inside the secret match. Sealed data is opaque by design -- that is the
point of sealing it -- so a wrong value still has to be found by running the
task. This pins the thing that was actually wrong both times: existence, in the
right namespace.
"""

import fnmatch
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DAGS = REPO / "dags"
MANIFESTS = REPO / "platform-gitops" / "manifests"
APPS = REPO / "platform-gitops" / "apps"

#: Secrets the Airflow Helm release creates itself, so no manifest of ours
#: seals them. Named rather than pattern-matched: an unexpected airflow-*
#: secret should fail this test, not be waved through.
CHART_OWNED = {
    "airflow-git-credentials",
    "airflow-s3-credentials",
    "airflow-sso-credentials",
}

#: SECRETS THAT EXIST IN THE CLUSTER BUT IN NO MANIFEST. This is drift, not an
#: exemption, and each entry is a known debt rather than a decision:
#:
#:   minio-credentials -- created by hand in the airflow namespace (118d old,
#:     two keys) and consumed by transform_itviec's dbt_build. It works today
#:     and would vanish if the namespace were rebuilt. Sealing it belongs to
#:     the paused Sealed Secrets migration, not to this test.
#:
#: Listed so the guard stays useful for everything else instead of being
#: deleted for one red line. Removing a name from here should mean sealing it,
#: never silencing it.
UNMANAGED_DEBT = {
    "minio-credentials",
}


def _dag_secret_refs() -> dict[str, set[Path]]:
    """Secret names referenced by a V1SecretKeySelector in any DAG file.

    Parsed with a regex rather than by importing the DAGs: importing needs
    airflow and the k8s client installed, which would make this test the
    heaviest in the suite to check something textual.
    """
    refs: dict[str, set[Path]] = {}
    pattern = re.compile(r"""V1SecretKeySelector\([^)]*?name=["']([\w.-]+)["']""",
                         re.S)
    for path in sorted(DAGS.glob("*.py")):
        for name in pattern.findall(path.read_text()):
            refs.setdefault(name, set()).add(path)
    return refs


def _sealed_into_airflow() -> set[str]:
    """SealedSecret names this repo seals into the `airflow` namespace.

    The namespace on the SealedSecret is what matters: sealing is namespace-
    bound, so a copy in `webapp` does nothing for a pod in `airflow`. That
    distinction is exactly what the second occurrence got wrong.
    """
    out: set[str] = set()
    for path in sorted(MANIFESTS.glob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "SealedSecret":
                continue
            meta = doc.get("metadata") or {}
            if meta.get("namespace") == "airflow":
                out.add(meta.get("name"))
    return out


def test_every_dag_secret_is_sealed_into_the_airflow_namespace():
    """A DAG reading a secret nothing seals into airflow cannot start its pod.

    If this fails: add a SealedSecret manifest with `namespace: airflow` for
    the named secret, plus the small ArgoCD Application that serves it (see
    apps/clickhouse-credentials.yaml for the shape -- the `airflow`
    Application is a Helm source and cannot carry a loose manifest).
    """
    refs = _dag_secret_refs()
    assert refs, "no secretKeyRef found in dags/ -- has the regex gone stale?"

    sealed = _sealed_into_airflow()
    missing = {
        name: sorted(p.name for p in paths)
        for name, paths in refs.items()
        if name not in sealed
        and name not in CHART_OWNED
        and name not in UNMANAGED_DEBT
    }
    assert not missing, (
        "these DAG secrets are not sealed into the airflow namespace, so the "
        f"task pod cannot be created: {missing}"
    )


def _application_includes() -> list[str]:
    """Every `directory.include` glob across the ArgoCD Applications."""
    includes = []
    for path in sorted(APPS.glob("*.yaml")):
        try:
            app = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(app, dict) or app.get("kind") != "Application":
            continue
        source = (app.get("spec") or {}).get("source") or {}
        include = (source.get("directory") or {}).get("include")
        if include:
            includes.append(include)
    return includes


def _sealed_secret_manifests() -> list[str]:
    """Filenames under manifests/ that declare at least one SealedSecret."""
    return [path.name for path in sorted(MANIFESTS.glob("*.yaml"))
            if "kind: SealedSecret" in path.read_text()]


@pytest.mark.parametrize("manifest", _sealed_secret_manifests())
def test_every_sealed_secret_manifest_has_an_application_serving_it(manifest):
    """A manifest ArgoCD never syncs is a file, not a secret.

    The manifests directory is served by per-file `directory.include`
    Applications, so a new manifest with no Application is invisible to the
    cluster -- which looks exactly like the missing-secret bug this file was
    added to catch. Worse than invisible, in fact: the secret still EXISTS,
    because whoever added it ran `kubectl apply` once by hand and saw it work.
    Nothing then keeps git and the cluster honest with each other.

    THIS IS WHY THE CHECK IS NOT SCOPED TO ONE NAMESPACE. Its first version
    only walked the secrets sealed into `airflow`, and reported four orphans.
    There were seven: minio-root-sealed, oauth2-proxy-cloudbeaver-sealed and
    clickhouse-server-sealed seal into `database` and so were invisible to the
    test -- including the ClickHouse server's own password, added by the
    rotation that was supposed to close this class of bug out. Parametrising
    over the manifests rather than over one namespace's secret names is what
    makes the guard total.

    TWO THINGS THIS DELIBERATELY DOES NOT REQUIRE, both learned by getting
    them wrong first:

      * that the serving Application's destination matches the manifest's
        namespace. ArgoCD applies a manifest into the namespace the MANIFEST
        declares; the Application's destination is only the default for
        manifests that name none. So webapp-postgres-sealed-dsn.yaml is
        legitimately served by the webapp-scoped webapp-postgres Application
        while sealing into airflow.
      * that the include is an exact filename. `include` takes a glob, and
        'webapp-postgres*.yaml' is how that same Application picks up the
        postgres manifest and its SealedSecrets together.
    """
    includes = _application_includes()
    assert any(fnmatch.fnmatch(manifest, pattern) for pattern in includes), (
        f"{manifest!r} declares a SealedSecret but no ArgoCD Application "
        f"includes it, so ArgoCD never syncs it; the include globs are "
        f"{sorted(includes)}"
    )
