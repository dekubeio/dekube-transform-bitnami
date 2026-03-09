"""bitnami — h2c transform.

Detects Bitnami Redis, PostgreSQL, and Keycloak services and applies
workarounds so they run in compose without manual overrides.

Every modification is printed to stderr for transparency.
"""

import base64
import sys


def _log(msg):
    print(f"  [bitnami] {msg}", file=sys.stderr)


def _secret_value(secret, key):
    """Decode a value from a K8s Secret manifest."""
    val = (secret.get("stringData") or {}).get(key)
    if val is not None:
        return val
    val = (secret.get("data") or {}).get(key)
    if val is not None:
        try:
            return base64.b64decode(val).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return val
    return None


def _is_bitnami_image(svc, name_fragment):
    """Check if a service uses a Bitnami image matching name_fragment."""
    image = svc.get("image", "")
    return "bitnami" in image and name_fragment in image


def _find_secret(secrets, candidates):
    """Find the first matching Secret from a list of candidate names."""
    for name in candidates:
        if name in secrets:
            return name, secrets[name]
    return None, None


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

def _fix_redis(svc_name, svc, ctx):
    """Replace Bitnami Redis with stock redis:7-alpine."""
    # Find the redis secret — typically <prefix>-redis or <release>-redis
    # where svc_name is like <prefix>-redis-master
    prefix = svc_name.replace("-redis-master", "").replace("-master", "")
    candidates = [f"{prefix}-redis", prefix, svc_name]
    sec_name, secret = _find_secret(ctx.secrets, candidates)

    password = None
    if secret:
        password = _secret_value(secret, "redis-password")

    svc["image"] = "redis:7-alpine"
    _log(f"{svc_name}: image → redis:7-alpine")

    svc.pop("entrypoint", None)
    _log(f"{svc_name}: removed Bitnami entrypoint")

    cmd = ["redis-server"]
    if password:
        cmd.extend(["--requirepass", password])
        _log(f"{svc_name}: password set from Secret '{sec_name}'")
    else:
        _log(f"{svc_name}: ⚠ no redis-password found, running without auth")
    svc["command"] = cmd

    volume_root = ctx.config.get("volume_root", "./data")
    svc["volumes"] = [f"{volume_root}/{svc_name}:/data"]
    _log(f"{svc_name}: volume → {volume_root}/{svc_name}:/data")

    svc.pop("environment", None)
    _log(f"{svc_name}: removed Bitnami environment")


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

def _fix_postgresql(svc_name, svc, ctx):
    """Fix Bitnami PostgreSQL volume mounts."""
    volume_root = ctx.config.get("volume_root", "./data")

    # Preserve existing non-data mounts (configmaps for init-scripts, etc.)
    existing = [v for v in (svc.get("volumes") or [])
                if ":/bitnami/postgresql" not in v
                and ":/opt/bitnami/postgresql/secrets" not in v]

    volumes = [f"{volume_root}/{svc_name}:/bitnami/postgresql"]
    _log(f"{svc_name}: data volume → /bitnami/postgresql")

    volumes.append(f"./secrets/{svc_name}:/opt/bitnami/postgresql/secrets:ro")
    _log(f"{svc_name}: secrets mount → /opt/bitnami/postgresql/secrets")

    svc["volumes"] = volumes + existing


# ---------------------------------------------------------------------------
# Keycloak
# ---------------------------------------------------------------------------

def _fix_keycloak(svc_name, svc, ctx):
    """Fix Bitnami Keycloak secrets and environment."""
    # The entrypoint reads passwords from files — inject them as env vars
    # so Keycloak can start even if the secret file mounts are missing.
    prefix = svc_name.replace("-keycloak", "").replace("keycloak", "").strip("-")
    if prefix:
        sec_candidates = [f"{prefix}-keycloak", svc_name, "keycloak"]
    else:
        sec_candidates = [svc_name, "keycloak"]

    sec_name, secret = _find_secret(ctx.secrets, sec_candidates)
    if secret:
        admin_pw = _secret_value(secret, "admin-password")
        if admin_pw:
            svc.setdefault("environment", {})["KC_BOOTSTRAP_ADMIN_PASSWORD"] = admin_pw
            _log(f"{svc_name}: KC_BOOTSTRAP_ADMIN_PASSWORD set from Secret '{sec_name}'")

    # DB password — look in the keycloak-postgresql secret
    db_candidates = [f"{prefix}-postgresql" if prefix else "keycloak-postgresql",
                     "keycloak-postgresql"]
    db_sec_name, db_secret = _find_secret(ctx.secrets, db_candidates)
    if db_secret:
        db_pw = _secret_value(db_secret, "password")
        if db_pw:
            svc.setdefault("environment", {})["KC_DB_PASSWORD"] = db_pw
            _log(f"{svc_name}: KC_DB_PASSWORD set from Secret '{db_sec_name}'")


def _fix_keycloak_init(svc_name, compose_services):
    """Remove the Bitnami prepare-write-dirs init that fails on emptyDir."""
    # Find and remove init services that copy to /emptydir
    to_remove = []
    for name in compose_services:
        if svc_name.replace("-keycloak", "") in name and "init-prepare-write-dirs" in name:
            to_remove.append(name)
    for name in to_remove:
        del compose_services[name]
        _log(f"{name}: removed (emptyDir copy fails in compose)")


# ---------------------------------------------------------------------------
# Transform entry point
# ---------------------------------------------------------------------------

class BitnamiWorkarounds:  # pylint: disable=too-few-public-methods  # contract: one class, one method
    """Auto-fix Bitnami Redis, PostgreSQL, and Keycloak for compose."""

    name = "bitnami"
    priority = 1500  # after converters, before flatten-internal-urls (2000)

    def transform(self, compose_services, ingress_entries, ctx):  # pylint: disable=unused-argument  # Transform contract signature
        """Apply Bitnami-specific workarounds to compose services."""
        user_overrides = ctx.config.get("overrides", {})

        for svc_name in list(compose_services):
            if svc_name in user_overrides:
                continue  # user override takes precedence

            svc = compose_services[svc_name]

            if _is_bitnami_image(svc, "redis"):
                _fix_redis(svc_name, svc, ctx)
            elif _is_bitnami_image(svc, "postgresql"):
                _fix_postgresql(svc_name, svc, ctx)
            elif _is_bitnami_image(svc, "keycloak"):
                _fix_keycloak(svc_name, svc, ctx)
                _fix_keycloak_init(svc_name, compose_services)
