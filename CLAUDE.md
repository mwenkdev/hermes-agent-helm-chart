# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An **unofficial community Helm chart** (no application source code) that packages [Nous Research's Hermes Agent](https://github.com/nousresearch/hermes-agent) for Kubernetes. Everything here is Helm templates, a values schema, and helm-unittest suites. Published as an OCI chart to `ghcr.io/<owner>/hermes-agent`.

## Commands

```bash
helm lint .                     # template syntax + schema lint
helm unittest .                 # full suite (tests/*_test.yaml), what CI runs
helm unittest -f 'tests/service_test.yaml' .   # single suite
helm unittest . -t 'renders a Deployment by default'   # single test by `it:` name

helm template hermes .                                  # render defaults
helm template hermes . -f tests/test-values.yaml        # render a scenario
helm template hermes . --include-crds -f tests/operator-values.yaml
```

Requires the `helm-unittest` plugin (CI pins v1.1.2):
`helm plugin install https://github.com/helm-unittest/helm-unittest --version v1.1.2 --verify=false`

The README's "Verification" section is **stale** — it lists `ci/*.yaml` value files and a `ci/verify.sh` that no longer exist. Those fixtures now live in `tests/` and are driven by `helm unittest`.

## Architecture

### Two mutually exclusive modes, gated at the top of every template

Every workload template begins with `{{- if not .Values.operator.enabled }}`.

- **Direct mode** (default): Helm owns the Deployment, PVC, Secret, ConfigMap, Service, Ingress, VirtualService, RBAC, NetworkPolicy, PDB.
- **Operator mode** (`operator.enabled=true`): *all* direct workload templates are suppressed and the chart renders only `HermesTenant` custom resources (`templates/hermes-tenants.yaml`, CRD in `crds/`). No controller is bundled — the chart is purely a CRD/CR producer for a cluster that already runs a compatible controller.

**When adding a new workload template, wrap it in the same `not .Values.operator.enabled` guard**, or operator mode will leak direct resources.

### Two-layer validation — keep both in sync

1. **`values.schema.json`** runs *before* templates render. Its `allOf`/`if`/`then` blocks enforce the cross-field rules (persistence ⇒ `replicaCount: 1` + `strategy.type: Recreate`; `service.enabled` ⇒ ports or an enabled listener; `virtualService` hosts/gateways; `telegramWebhook.url`; `externalSecret.secretStoreRef.name`; `tenantIsolation` ⇒ `tenant.id`; operator ⇒ `controllerClass`).
2. **`{{ fail "..." }}` guards** inside `deployment.yaml`, `service.yaml`, `ingress.yaml`, `virtualservice.yaml`, `networkpolicy.yaml`, `rbac.yaml` restate most of the same rules as a backstop, plus rules the schema can't express (e.g. `apiServer.enabled` requires `secrets.API_SERVER_KEY` unless a secret is managed externally).

Because the schema fires first, `tests/validation_test.yaml` asserts on **schema** error text (`minItems`, `minLength`, `value must be 1`, JSON-pointer fragments like `secretStoreRef/name`) — not on the `fail` strings. Adding a constraint usually means touching the schema, the template guard, and that test.

### State safety is the chart's central invariant

Hermes keeps mutable state in `HERMES_HOME` (`persistence.mountPath`, default `/opt/data`), treated as a **single-writer** volume: with `persistence.enabled`, `replicaCount` must be 1 and `strategy.type` must be `Recreate`. Horizontal scaling is done with **multiple releases / tenants**, never more replicas.

### Service port derivation

`_helpers.tpl` defines `hermes-agent.servicePorts` — the single source of truth: explicit `service.ports` if non-empty, else auto-derived entries for whichever of `apiServer` / `webhook` / `telegramWebhook` is enabled. It returns JSON, consumed via `| fromJsonArray`. `hermes-agent.primaryServicePortNumber` picks entry 0 for ingress/VirtualService routing and `fail`s if nothing is exposed.

Caveat: `service.yaml`, `ingress.yaml`, and `virtualservice.yaml` currently **re-implement** this derivation inline instead of calling the helper. Change the logic in one place and you must change it in all four.

### Deployment container layout

- **`bootstrap-config` init container** copies `config.yaml` / `SOUL.md` from the bootstrap ConfigMap into `HERMES_HOME`. `bootstrap.overwrite=false` (default) seeds only when the file is absent, so pod restarts don't clobber runtime state.
- **`npm-install` init container** (only when `npmPackages` is non-empty) installs into `$HERMES_HOME/npm-global` and short-circuits on a sha256 hash of the package list stored in the volume; `PATH`/`NODE_PATH`/`NPM_CONFIG_PREFIX` are rewritten on the main container.
- The image entrypoint already execs `hermes`, so `args` are appended to it (default `["gateway", "run"]`); `command` is an escape hatch.
- **s6-overlay accommodations** are deliberate, not accidental: `S6_KEEP_ENV=1` and `S6_READ_ONLY_ROOT=1` in `extraEnv`, `S6_YES_I_WANT_A_WORLD_WRITABLE_RUN_BECAUSE_KUBERNETES=1` hardcoded, emptyDir mounts at `/tmp` and `/run`, and `readOnlyRootFilesystem: false`. These exist because the container runs non-root (UID/GID 10000) with all capabilities dropped. A `deployment_test.yaml` case pins `readOnlyRootFilesystem: false` — don't "harden" it without understanding s6.
- Pod annotations carry `checksum/config` and `checksum/secret` (or `checksum/external-secret`) so config changes roll the pod.

### Secrets: three exclusive paths

`hermes-agent.secretName` resolves in order: `externalSecret.target.name` → `secrets.existingSecret` → generated `<fullname>-secrets`. `deployment.yaml` explicitly `fail`s if `externalSecret.enabled` is combined with `secrets.existingSecret` or with any inline `secrets.*` value. The chart-managed Secret renders only when at least one inline value is non-empty; inline values go through `tpl`.

## Versioning gotcha

`Chart.yaml` `version` stays `0.0.0-dev` in git — the release workflow injects the real semver from the auto-generated tag at `helm package` time. Only `appVersion` is bumped by hand (Renovate tracks it via the comment above it).

Bumping the Hermes version means editing **three** places, which are not linked:
1. `Chart.yaml` `appVersion`
2. `values.yaml` `image.tag` (deliberately pinned explicitly rather than falling back to `.Chart.AppVersion`)
3. the exact-image assertion in `tests/deployment_test.yaml`

`renovate.json` also still has a custom manager pointing at `.github/workflows/docker-release.yaml`, which was removed when image building left this repo — it is a no-op.

## CI

- `.github/workflows/pr.yaml` — on PRs to `main` and pushes to `main`: `helm lint .` + `helm unittest .`.
- `.github/workflows/release.yaml` — on push to the **`release`** branch: auto-tags (default patch bump), packages with that version, pushes to GHCR, creates a GitHub Release. Merging to `main` does not publish.

## Conventions

- Helper names are namespaced `hermes-agent.*` (matches `.Chart.Name`, not the release name).
- Tenant identity is the label `tenant.hermes.ai/id`, emitted by `hermes-agent.tenantLabels` and folded into `hermes-agent.labels`; `tenantIsolation` builds NetworkPolicy and pod anti-affinity from it.
- `.helmignore` excludes `tests/` and `docs/` from the packaged chart.
- New test fixtures go in `tests/` as `*-values.yaml`; suites as `*_test.yaml` with a `suite:` name and explicit `template:` per assertion.
