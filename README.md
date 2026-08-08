# Unofficial Hermes Agent Helm Chart

> [!IMPORTANT]
> This is an **unofficial community Helm chart** for [Nous Research's Hermes Agent](https://github.com/nousresearch/hermes-agent). It is maintained independently from the upstream Hermes project.

The chart defaults to the official `nousresearch/hermes-agent` image. No image is built or published by this repository.

## What the chart deploys

In its default (direct) mode the chart renders:

- a `Deployment` running `hermes gateway run`, with an init container that seeds `config.yaml` and optional `SOUL.md` into `HERMES_HOME`
- a `PersistentVolumeClaim` for `HERMES_HOME` (enabled by default)
- a `ConfigMap` for bootstrap content, and a `Secret` when inline secret values are set
- a `ServiceAccount` (created by default)

Optional, all disabled by default: `Service`, `Ingress`, Istio `VirtualService`, `ExternalSecret`, `NetworkPolicy`, `Role`/`RoleBinding` (or cluster-scoped), `PodDisruptionBudget`, and arbitrary resources via `extraObjects`.

The chart also ships a `HermesTenant` CRD and an operator mode (see [Operator mode](#operator-mode)).

## Installation

Install from the OCI registry:

```bash
helm install hermes oci://ghcr.io/mwenkdev/hermes-agent \
  --namespace hermes --create-namespace
```

Or from a clone of this repository:

```bash
helm install hermes . --namespace hermes --create-namespace
```

Minimal values:

```yaml
secrets:
  OPENROUTER_API_KEY: sk-or-...

config:
  values:
    model:
      default: anthropic/claude-opus-4.6
```

```bash
helm install hermes oci://ghcr.io/mwenkdev/hermes-agent \
  --namespace hermes --create-namespace -f values.yaml
```

## Important values

| Key | Default | Notes |
| --- | --- | --- |
| `image.repository` | `nousresearch/hermes-agent` | |
| `image.tag` | `v2026.8.3` | Falls back to `.Chart.AppVersion` if empty |
| `replicaCount` | `1` | See [Persistence](#persistence-and-the-replicacount-rule) |
| `strategy.type` | `Recreate` | |
| `args` | `[gateway, run]` | Appended to the image entrypoint, which already execs `hermes` |
| `persistence.enabled` | `true` | PVC mounted at `persistence.mountPath` |
| `persistence.mountPath` | `/opt/data` | Used as `HERMES_HOME` |
| `persistence.size` | `5Gi` | |
| `bootstrap.enabled` | `true` | Seeds `config.yaml` and `SOUL.md` |
| `bootstrap.overwrite` | `false` | When `false`, seeds only if the file is absent |
| `config.values` | `{}` | Structured YAML rendered into `config.yaml` |
| `config.raw` | `""` | Raw templated YAML, takes precedence over `config.values` |
| `apiServer.enabled` | `false` | OpenAI-compatible API server, port `8642` |
| `webhook.enabled` | `false` | Port `8644` |
| `telegramWebhook.enabled` | `false` | Port `8443`, requires `telegramWebhook.url` |
| `service.enabled` | `false` | |
| `ingress.enabled` / `virtualService.enabled` | `false` | Both require `service.enabled=true` |
| `serviceAccount.create` | `true` | `automountServiceAccountToken` defaults to `false` |
| `rbac.create` | `false` | Requires at least one entry in `rbac.rules` |
| `networkPolicy.enabled` | `false` | |
| `pdb.enabled` | `false` | |
| `npmPackages` | `[]` | Installed into the volume, exposed via `PATH` and `NODE_PATH` |
| `probes.liveness` / `readiness` / `startup` | `{}` | Raw probe specs copied onto the container |
| `resources` | `{}` | See [Resources](#resources) |

Pod and container security contexts default to non-root (UID/GID 10000), all capabilities dropped, and the `RuntimeDefault` seccomp profile. `readOnlyRootFilesystem` defaults to `false` because the image runs an s6-overlay supervision tree that writes to `/run`.

`values.schema.json` validates values before templates render, so misconfigurations fail at install time rather than producing broken manifests.

## Persistence and the replicaCount rule

Hermes stores mutable state under `HERMES_HOME`, so the chart treats the volume as single-writer. When `persistence.enabled=true`:

- `replicaCount` must be `0` or `1`. A value of `2` or more is rejected.
- `strategy.type` must be `Recreate`.

`replicaCount: 0` is valid and scales the deployment down without removing the release or its volume.

When `persistence.enabled=false` the volume becomes an `emptyDir` and neither restriction applies, so higher replica counts are accepted. State is not retained across pod restarts in that mode.

To run more than one Hermes instance with persistence, use multiple releases rather than scaling one release.

Use `persistence.existingClaim` to bind a pre-provisioned volume:

```yaml
persistence:
  enabled: true
  existingClaim: hermes-data
```

## Secrets

Three mutually exclusive options.

**1. Chart-managed Secret.** Set any key under `secrets`. Only non-empty values are rendered.

```yaml
secrets:
  OPENROUTER_API_KEY: sk-or-...
```

**2. Existing Secret.** The chart consumes it via `envFrom` and renders no Secret of its own.

```yaml
secrets:
  existingSecret: hermes-secrets
```

**3. ExternalSecret.** Renders an `external-secrets.io/v1beta1` `ExternalSecret` for the External Secrets Operator.

```yaml
externalSecret:
  enabled: true
  secretStoreRef:
    kind: ClusterSecretStore
    name: platform-secrets
  data:
    - secretKey: OPENROUTER_API_KEY
      remoteRef:
        key: hermes
        property: OPENROUTER_API_KEY
```

`externalSecret.enabled` cannot be combined with `secrets.existingSecret` or with inline `secrets.*` values. `dataFrom` is supported as an alternative to `data`.

Enabling `apiServer` requires `secrets.API_SERVER_KEY` unless one of the external options is in use. The same applies to `secrets.TELEGRAM_BOT_TOKEN` when `telegramWebhook` is enabled.

## Exposure

`service.enabled=true` requires either explicit `service.ports` or at least one enabled listener (`apiServer`, `webhook`, `telegramWebhook`). When `service.ports` is empty the ports are derived from the enabled listeners.

```yaml
apiServer:
  enabled: true

service:
  enabled: true

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: hermes.example.com
      paths:
        - path: /
          pathType: Prefix
```

For Istio, `virtualService.enabled=true` requires at least one entry in both `virtualService.gateways` and `virtualService.hosts`.

## Browser shared memory

Chromium and Playwright need more shared memory than the default container allocation. Enable a memory-backed volume at `/dev/shm`:

```yaml
browser:
  sharedMemory:
    enabled: true
    sizeLimit: 1Gi
```

Defaults are `enabled: false`, `mountPath: /dev/shm`, `sizeLimit: 1Gi`. The volume counts against the pod memory limit.

## Resources

`resources` is empty by default and is copied onto the container verbatim.

```yaml
resources:
  requests:
    memory: 1Gi
  limits:
    memory: 4Gi
```

## Operator mode

`operator.enabled=true` suppresses all direct workload templates and renders only `HermesTenant` custom resources from `operator.tenants`. This repository does not bundle a controller, so use this mode only if your cluster already runs one that reconciles `HermesTenant`. Direct mode remains the default.

```yaml
operator:
  enabled: true
  controllerClass: hermes.ai/default
  tenants:
    - name: tenant-a
      namespace: tenant-a
      tenantId: tenant-a
      chartValues:
        apiServer:
          enabled: true
```

CRDs in `crds/` are installed by Helm on first install and are not upgraded or deleted by Helm afterwards. Render them with `helm template . --include-crds`.

## Upgrade and versioning

- `appVersion` tracks the upstream Hermes release and is the default image tag source. `image.tag` is pinned explicitly in `values.yaml`.
- The chart `version` is `0.0.0-dev` in git. Releases are packaged on pushes to the `release` branch, which auto-tags, publishes to GHCR, and creates a GitHub release.
- Because persistence uses `Recreate`, upgrades stop the old pod before starting the new one, so expect brief downtime.
- With `bootstrap.overwrite: false` (the default), an upgrade does not overwrite `config.yaml` or `SOUL.md` already present in the volume. Set it to `true` to make Helm the source of truth.
- Config and secret changes roll the pod automatically via checksum annotations.

## Development and testing

Requires the [helm-unittest](https://github.com/helm-unittest/helm-unittest) plugin:

```bash
helm plugin install https://github.com/helm-unittest/helm-unittest
```

```bash
helm lint .
helm unittest .
```

Render manifests locally:

```bash
helm template hermes .
helm template hermes . -f tests/test-values.yaml
```

Test suites live in `tests/*_test.yaml` with scenario values in `tests/*-values.yaml`. CI runs `helm lint .` and `helm unittest .` on pull requests to `main`.

## Credits
This chart was originally based on the community Helm chart by MichaelSp, which was itself based on the chart by realsigridjin. It has since been substantially updated and is now maintained independently.