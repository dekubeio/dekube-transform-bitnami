"""bitnami — dekube transform.

Detects Bitnami Redis, PostgreSQL, and Keycloak services and applies
workarounds so they run in compose without manual overrides.

Every modification is printed to stderr for transparency.
"""

from dekube import secret_value, log  # pylint: disable=import-error  # h2c resolves at runtime


# ---------------------------------------------------------------------------
# Transform entry point
# ---------------------------------------------------------------------------

class BitnamiWorkarounds:  # pylint: disable=too-few-public-methods  # contract: one class, one method
    """Auto-fix Bitnami Redis, PostgreSQL, and Keycloak for compose."""

    name = "bitnami"
    priority = 1500  # after converters, before flatten-internal-urls (2000)

    @staticmethod
    def _is_bitnami_image(svc, name_fragment):
        """Check if a service uses a Bitnami image matching name_fragment."""
        image = svc.get("image", "")
        return "bitnami" in image and name_fragment in image

    @staticmethod
    def _find_secret(secrets, candidates):
        """Find the first matching Secret from a list of candidate names."""
        for name in candidates:
            if name in secrets:
                return name, secrets[name]
        return None, None

    # ---------------------------------------------------------------------------
    # Redis
    # ---------------------------------------------------------------------------

    def _fix_redis(self, svc_name, svc, ctx):
        """Replace Bitnami Redis with stock redis:7-alpine."""
        # Find the redis secret — typically <prefix>-redis or <release>-redis
        # where svc_name is like <prefix>-redis-master
        prefix = svc_name.replace("-redis-master", "").replace("-master", "")
        candidates = [f"{prefix}-redis", prefix, svc_name]
        sec_name, secret = self._find_secret(ctx.secrets, candidates)

        password = None
        if secret:
            password = secret_value(secret, "redis-password")

        svc["image"] = "redis:7-alpine"
        log(self.name, f"{svc_name}: image → redis:7-alpine")

        svc.pop("entrypoint", None)
        log(self.name, f"{svc_name}: removed Bitnami entrypoint")

        cmd = ["redis-server"]
        if password:
            cmd.extend(["--requirepass", password])
            log(self.name, f"{svc_name}: password set from Secret '{sec_name}'")
        else:
            log(self.name, f"{svc_name}: ⚠ no redis-password found, running without auth")
        svc["command"] = cmd

        volume_root = ctx.config.get("volume_root", "./data")
        svc["volumes"] = [f"{volume_root}/{svc_name}:/data"]
        log(self.name, f"{svc_name}: volume → {volume_root}/{svc_name}:/data")

        svc.pop("environment", None)
        log(self.name, f"{svc_name}: removed Bitnami environment")

    # ---------------------------------------------------------------------------
    # PostgreSQL
    # ---------------------------------------------------------------------------

    def _fix_postgresql(self, svc_name, svc, ctx):
        """Fix Bitnami PostgreSQL volume mounts."""
        volume_root = ctx.config.get("volume_root", "./data")

        # Preserve existing non-data mounts (configmaps for init-scripts, etc.)
        existing = [v for v in (svc.get("volumes") or [])
                    if ":/bitnami/postgresql" not in v
                    and ":/opt/bitnami/postgresql/secrets" not in v]

        volumes = [f"{volume_root}/{svc_name}:/bitnami/postgresql"]
        log(self.name, f"{svc_name}: data volume → /bitnami/postgresql")

        volumes.append(f"./secrets/{svc_name}:/opt/bitnami/postgresql/secrets:ro")
        log(self.name, f"{svc_name}: secrets mount → /opt/bitnami/postgresql/secrets")

        svc["volumes"] = volumes + existing

    # ---------------------------------------------------------------------------
    # Keycloak
    # ---------------------------------------------------------------------------

    def _fix_keycloak(self, svc_name, svc, ctx):
        """Fix Bitnami Keycloak secrets and environment."""
        # The entrypoint reads passwords from files — inject them as env vars
        # so Keycloak can start even if the secret file mounts are missing.
        prefix = svc_name.replace("-keycloak", "").replace("keycloak", "").strip("-")
        if prefix:
            sec_candidates = [f"{prefix}-keycloak", svc_name, "keycloak"]
        else:
            sec_candidates = [svc_name, "keycloak"]

        sec_name, secret = self._find_secret(ctx.secrets, sec_candidates)
        if secret:
            admin_pw = secret_value(secret, "admin-password")
            if admin_pw:
                svc.setdefault("environment", {})["KC_BOOTSTRAP_ADMIN_PASSWORD"] = admin_pw
                log(self.name, f"{svc_name}: KC_BOOTSTRAP_ADMIN_PASSWORD set from Secret '{sec_name}'")

        # DB password — look in the keycloak-postgresql secret
        db_candidates = [f"{prefix}-postgresql" if prefix else "keycloak-postgresql",
                         "keycloak-postgresql"]
        db_sec_name, db_secret = self._find_secret(ctx.secrets, db_candidates)
        if db_secret:
            db_pw = secret_value(db_secret, "password")
            if db_pw:
                svc.setdefault("environment", {})["KC_DB_PASSWORD"] = db_pw
                log(self.name, f"{svc_name}: KC_DB_PASSWORD set from Secret '{db_sec_name}'")

    def _fix_keycloak_init(self, svc_name, compose_services):
        """Remove the Bitnami prepare-write-dirs init that fails on emptyDir."""
        # Find and remove init services that copy to /emptydir
        to_remove = []
        for name in compose_services:
            if svc_name.replace("-keycloak", "") in name and "init-prepare-write-dirs" in name:
                to_remove.append(name)
        for name in to_remove:
            del compose_services[name]
            log(self.name, f"{name}: removed (emptyDir copy fails in compose)")

    def transform(self, compose_services, ingress_entries, ctx):  # pylint: disable=unused-argument  # Transform contract signature
        """Apply Bitnami-specific workarounds to compose services."""
        user_overrides = ctx.config.get("overrides", {})

        for svc_name in list(compose_services):
            if svc_name in user_overrides:
                continue  # user override takes precedence

            svc = compose_services[svc_name]

            if self._is_bitnami_image(svc, "redis"):
                self._fix_redis(svc_name, svc, ctx)
            elif self._is_bitnami_image(svc, "postgresql"):
                self._fix_postgresql(svc_name, svc, ctx)
            elif self._is_bitnami_image(svc, "keycloak"):
                self._fix_keycloak(svc_name, svc, ctx)
                self._fix_keycloak_init(svc_name, compose_services)
