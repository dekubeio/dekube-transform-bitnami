# bitnami

![vibe coded](https://img.shields.io/badge/vibe-coded-ff69b4)
![python 3](https://img.shields.io/badge/python-3-3776AB)
![heresy: 0/10](https://img.shields.io/badge/heresy-0%2F10-blueviolet)

dekube transform that detects Bitnami Redis, PostgreSQL, and Keycloak services and applies workarounds so they run in compose without manual overrides.

## Why

Bitnami charts wrap standard images in custom entrypoints, init containers, and volume conventions that assume a full Kubernetes environment. In compose, these assumptions break: entrypoint chains fail, volume paths don't line up, Secret file mounts don't exist. The workarounds are well-known and documented in [common charts](https://helmfile2compose.dekube.io/known-workarounds/common-charts/) — this transform applies them automatically.

## What it does

Every modification is logged to stderr for transparency.

### Redis (`bitnami/redis`)

- Replaces the Bitnami image with stock `redis:7-alpine`
- Removes the Bitnami entrypoint and environment
- Sets `redis-server --requirepass <password>` from the K8s Secret
- Mounts a data volume at `/data`

### PostgreSQL (`bitnami/postgresql`)

- Fixes volume mounts to `/bitnami/postgresql` (where the Bitnami entrypoint expects data)
- Mounts generated secrets to `/opt/bitnami/postgresql/secrets`

### Keycloak (`bitnami/keycloak`)

- Injects `KC_BOOTSTRAP_ADMIN_PASSWORD` and `KC_DB_PASSWORD` as environment variables from K8s Secrets
- Removes the `prepare-write-dirs` init container (fails on emptyDir in compose)

## User overrides take precedence

If a service has a manual `overrides:` entry in `dekube.yaml`, the transform skips it. You can always override per-service if the automatic fix doesn't fit your setup.

## Install

```bash
python3 dekube-manager.py bitnami
```

Or add to `dekube.yaml`:

```yaml
depends:
  - bitnami
```

## Usage

The transform is loaded automatically via `--extensions-dir`. No configuration needed.

```bash
# Via dekube-manager run mode
python3 dekube-manager.py run -e compose

# Manual
python3 helmfile2compose.py --from-dir /tmp/rendered \
  --extensions-dir .dekube/extensions --output-dir .
```

Verify it loaded: `Loaded transforms: BitnamiWorkarounds` appears on stderr.

## Priority

150 (after converters, before flatten-internal-urls at 200).

## Code quality

*Last updated: 2026-02-23*

| Metric | Value |
|--------|-------|
| Pylint | 10.00/10 |
| Pyflakes | clean |
| Radon MI | 62.14 (A) |
| Radon avg CC | 4.1 (A) |

No C-rated functions.

## License

Public domain.
