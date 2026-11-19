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
    def _repo_name(image):
        """Extract the bare repository name (last path segment, no tag/digest) from an image ref."""
        repo = image.split("@", 1)[0]  # strip digest (…@sha256:…)
        head, sep, tail = repo.rpartition(":")
        if sep and "/" not in tail:  # tail is a tag, not a registry :port
            repo = head
        return repo.rsplit("/", 1)[-1]

    @staticmethod
    def _is_bitnami_image(svc, name_fragment):
        """Check if a service's image repo is exactly bitnami/<name_fragment>, any tag/digest/registry."""
        image = svc.get("image", "") or ""
        if "bitnami" not in image:
            return False
        return BitnamiWorkarounds._repo_name(image) == name_fragment

    @staticmethod
    def _mapped_data_path(svc, mount_paths, ctx):
        """Host path of an existing bind mount on one of mount_paths, if the user chose it.

        The engine already resolved the PVC through dekube.yaml `volumes:`, but this
        transform used to ignore that and mount <volume_root>/<service>. The first run
        auto-registers every PVC as `<claim>: {host_path: <claim>}`, so existing installs
        carry such an entry while their data sits in <volume_root>/<service>: honoring it
        would point them at an empty directory. Only an explicit mapping (host_path
        different from its key) is kept.
        """
        volume_root = ctx.config.get("volume_root", "./data")
        explicit = set()
        for claim, cfg in (ctx.config.get("volumes") or {}).items():
            host_path = (cfg or {}).get("host_path") if isinstance(cfg, dict) else None
            if host_path and host_path != claim:
                # same resolution as the engine's _resolve_host_path (not public API)
                if host_path == "$volume_root" or host_path.startswith("$volume_root/"):
                    host_path = volume_root + host_path[len("$volume_root"):]
                elif not host_path.startswith(("/", "./", "../")):
                    host_path = f"{volume_root}/{host_path}"
                explicit.add(host_path)
        for vol in svc.get("volumes") or []:
            if not isinstance(vol, str):
                continue
            host, sep, rest = vol.partition(":")
            if sep and host in explicit and rest.split(":", 1)[0] in mount_paths:
                return host
        return None

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
            # Compose interpolates $ in `command` (unlike `environment`, which the
            # engine escapes for us later) — escape it ourselves or auth breaks.
            cmd.extend(["--requirepass", password.replace("$", "$$")])
            log(self.name, f"{svc_name}: password set from Secret '{sec_name}'")
        else:
            log(self.name, f"{svc_name}: ⚠ no redis-password found, running without auth")
        svc["command"] = cmd

        volume_root = ctx.config.get("volume_root", "./data")
        host = (self._mapped_data_path(svc, ("/data", "/bitnami/redis/data"), ctx)
                or f"{volume_root}/{svc_name}")
        svc["volumes"] = [f"{host}:/data"]
        log(self.name, f"{svc_name}: volume → {host}:/data")

        svc.pop("environment", None)
        log(self.name, f"{svc_name}: removed Bitnami environment")

        # The chart probe runs /health/*.sh under /bin/bash, absent from the alpine image.
        # redis-cli exits 0 on any reply (NOAUTH included) and 1 when nothing listens.
        if svc.get("healthcheck"):
            svc["healthcheck"]["test"] = ["CMD", "redis-cli", "ping"]
            log(self.name, f"{svc_name}: healthcheck → redis-cli ping")

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

        host = (self._mapped_data_path(svc, ("/bitnami/postgresql",), ctx)
                or f"{volume_root}/{svc_name}")
        volumes = [f"{host}:/bitnami/postgresql"]
        log(self.name, f"{svc_name}: data volume → {host}:/bitnami/postgresql")

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
