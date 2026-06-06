# job-store Helm chart

Deploys the gunicorn'd [job-store](../README.md) image (Deployment + Service) to
a Kubernetes cluster (targets k3s 1.28+).

```bash
helm install job-store ./helm -n job-board --create-namespace -f values.yaml
```

Scoring credentials are required — see [Secrets](#secrets). Quick start:

```bash
helm install job-store ./helm -n job-board --create-namespace \
  --set secret.anthropicApiKey=sk-ant-... \
  --set-file secret.resumeYaml=./resume_details.yaml
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
| Persistence (PVC for `jobs.db`) | #36 | done — `jobs.db` is on a PVC (`persistence.enabled`); set `false` for a disposable `emptyDir` |
| Secret (`ANTHROPIC_API_KEY`, resume) | #37 | done — see [Secrets](#secrets) |
| Ingress + TLS | #38 | done — see [Ingress + TLS](#ingress--tls) (disabled by default) |
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
| `persistence.enabled` | `true` | PVC for `jobs.db`; `false` → disposable `emptyDir` |
| `persistence.storageClass` | `""` | `""` = cluster default; `"-"` = no dynamic provisioning; else the class name |
| `persistence.size` | `5Gi` | generous for a single-user inbox |
| `persistence.accessModes` | `["ReadWriteOnce"]` | RWO is correct for a single-writer deployment |
| `secret.create` | `true` | chart creates the Secret from the values below |
| `secret.existingSecret` | `""` | when set, use this externally-managed Secret instead |
| `secret.anthropicApiKey` | `""` | **required** when `create: true` |
| `secret.resumeYaml` | `""` | **required** when `create: true` |
| `ingress.enabled` | `false` | create an Ingress; off → `port-forward`/LoadBalancer |
| `ingress.className` | `""` | ingress class (e.g. `nginx`); `""` = cluster default |
| `ingress.annotations` | `{}` | cert-manager / controller hints |
| `ingress.hosts` | `[{host: job-store.local, paths: [{path: /, pathType: Prefix}]}]` | |
| `ingress.tls` | `[]` | list of `{secretName, hosts}` |

## Secrets

`ANTHROPIC_API_KEY` and the resume drive server-side scoring. The resume is a
Secret (not a ConfigMap) because it holds personal information. The Deployment
reads the key from `secretKeyRef` and mounts the resume read-only at
`/etc/job-store/resume.yaml` (`RESUME_PATH`). Either Secret form must carry the
same two keys: `anthropic-api-key` and `resume.yaml`.

**Chart-managed (quick start).** The chart creates the Secret:

```bash
helm install job-store ./helm -n job-board --create-namespace \
  --set secret.anthropicApiKey=sk-ant-... \
  --set-file secret.resumeYaml=./resume_details.yaml
```

With `secret.create: true`, `helm template`/`install` fails fast if either value
is unset.

**External Secret (production).** Manage the Secret out-of-band
(sealed-secrets / external-secrets / SOPS / Vault) and point the chart at it:

```bash
kubectl -n job-board create secret generic job-store-creds \
  --from-literal=anthropic-api-key=sk-ant-... \
  --from-file=resume.yaml=./resume_details.yaml   # or via your secrets operator

helm install job-store ./helm -n job-board \
  --set secret.create=false \
  --set secret.existingSecret=job-store-creds
```

## Ingress + TLS

Disabled by default. The Ingress is controller-agnostic — `className` and
`annotations` are pass-through, so it works with nginx-ingress, Traefik (k3s
default), etc.

> ⚠️ **The inbox has no authentication.** It surfaces personal data (applied
> history, branch names). Only expose it on a trusted network — a tailnet,
> WireGuard, or VPN — **never the public internet** until an auth layer lands.

**Recommended: cert-manager + nginx-ingress + Let's Encrypt.** With
[cert-manager](https://cert-manager.io/) installed and a `letsencrypt-prod`
`ClusterIssuer`, cert-manager provisions and renews the cert into the TLS Secret
named below:

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/ssl-redirect: "true"   # force HTTP -> HTTPS
  hosts:
    - host: job-store.internal.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: job-store-tls
      hosts:
        - job-store.internal.example.com
```

The HTTP→HTTPS redirect is controller-specific: `ssl-redirect` above for
nginx-ingress; on Traefik, redirect at the entrypoint (`web` → `websecure`) or
via a redirect middleware annotation. DNS for the host is yours to manage (or
let external-dns pick it up).

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
