# job-store Helm chart

Deploys the gunicorn'd [job-store](../README.md) image (Deployment + Service) to
a Kubernetes cluster (targets k3s 1.28+).

```bash
helm install job-store ./helm -n job-board --create-namespace -f values.yaml
```

Render without installing:

```bash
helm template job-store ./helm --debug
helm lint ./helm
```

## What this chart covers

This is the deployment + service **skeleton** (issue #35). Sibling concerns are
separate charts/issues, added incrementally:

| Concern | Issue | Status |
|---|---|---|
| Deployment + Service | #35 | this chart |
| Persistence (PVC for `jobs.db`) | #36 | pending — `jobs.db` is on an `emptyDir` and is **lost on pod restart** until then |
| Secret (`ANTHROPIC_API_KEY`, resume) | #37 | pending — scoring fails until wired; the inbox + probes work without it |
| Ingress + TLS | #38 | pending — reach the Service via `kubectl port-forward` for now |
| Poller CronJob | #39 | pending |

## Key values

| Key | Default | Notes |
|---|---|---|
| `image.repository` | `ghcr.io/dev-dull/job-store` | |
| `image.tag` | `latest` | pin to a `{git-sha}` in production |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | set if the GHCR package is private |
| `replicaCount` | `1` | **must stay 1** — see below |
| `resources` | 100m/128Mi req, 500m/512Mi limit | tune with profiling data |
| `service.type` / `service.port` | `ClusterIP` / `5000` | |
| `extraEnv` | `[]` | list of `{name, value}` appended to the container |

## Hard constraints

- **`replicaCount: 1` + `strategy: Recreate` are non-negotiable.** Two pods (or a
  rolling update's brief overlap) means two SQLite writers and a corrupt DB.
  Do not switch to `RollingUpdate` or scale up until the data layer becomes
  Postgres or similar.
- The pod runs **non-root with a read-only root filesystem**. Two `emptyDir`
  mounts provide the only writable paths: `/data` (the DB) and `/tmp`
  (gunicorn's worker heartbeat + control socket, via `HOME=/tmp`).

## Quick check after install

```bash
kubectl -n job-board port-forward svc/job-store 5000:5000
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/   # -> 200
```
