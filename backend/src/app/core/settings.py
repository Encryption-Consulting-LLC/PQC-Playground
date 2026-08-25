"""Application settings — read from environment variables and an optional .env file.

Login is always required — there is no anonymous/auto-connect mode. Every
visitor signs in with a provisioned account (username/password) or employee
SSO (OIDC). Admins, operators, and guests are all real accounts in the users
collection; the difference is the ``role`` the account carries — admin runs
the platform (``/admin`` console: accounts, the ESXi target, base images),
operator builds and deploys on the canvas, guest gets a restricted subset of
that (``core/authz.py``). Guests sign in with username/password only; SSO is
an operator/employee path.

Identity and the ESXi target are decoupled: who you are comes from
the users collection / the IdP, while *which* ESXi host gets used is the one
shared org-wide target stored in the Mongo settings document (seeded from the
``esxi_*`` env vars on first boot, admin-editable afterwards without a
restart — see ``core/esxi.py``).

Two secrets are required in every process (API and Celery worker) and are
fail-fast validated below; generate each with ``openssl rand -base64 32``:
  ``SESSION_SECRET``    — HMAC key for the backend-minted session JWTs.
  ``SETTINGS_ENC_KEY``  — base64 32-byte AES-256-GCM key encrypting the stored
                          ESXi password (``core/secrets.py``).
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.agent_binary import bundled_executor_agent_path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Identity layer.
    session_secret: str | None = None
    session_ttl_hours: int = 12
    settings_enc_key: str | None = None

    # Example guest account, seeded into the users collection at startup if
    # absent (idempotent — never overwrites an existing account or a password
    # an operator has since changed). Gives a fresh deploy a working
    # username/password login out of the box; disable by setting the password
    # empty. This is a low-privilege guest role, not an operator (bootstrap the
    # first operator with ``uv run create-admin``).
    example_guest_username: str = "guest"
    example_guest_password: str = "guest-playground"

    # Employee SSO — generic OIDC (Keycloak and Azure AD both fit). Enabled iff
    # issuer, client id/secret, and redirect URI are all set. Group values are
    # compared as exact strings against the ``oidc_group_claim`` claim, so
    # Keycloak group names and Azure AD group object-ids both work.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_redirect_uri: str | None = None
    oidc_group_claim: str = "groups"
    oidc_operator_groups: str = ""  # comma-separated
    oidc_guest_groups: str = ""  # comma-separated

    # First-boot seed for the shared ESXi target (written into the Mongo
    # settings document if absent there; NOT read at request time).
    esxi_host: str | None = None
    esxi_user: str | None = None
    esxi_password: str | None = None
    esxi_port: int = 443

    # First-boot seed for the guest subnet — same seed-only
    # semantics as the ESXi target above. The start/end range is inclusive
    # and must exclude the network, broadcast, and gateway addresses; the
    # backend pre-seeds one IP-pool document per address in the range.
    guest_ip_start: str | None = None
    guest_ip_end: str | None = None
    guest_prefix: int = 24
    guest_gateway: str | None = None
    guest_dns1: str | None = None
    guest_dns2: str | None = None
    guest_dns_suffix: str | None = None
    # Sanity cap on the number of addresses the guest range may span (one Mongo
    # ``ip_pool`` document is pre-seeded per address). Guards against a typo'd
    # range seeding a runaway collection; raise it for larger guest subnets.
    guest_pool_max_size: int = 8192

    # Golden image used by guided deploys. These values seed the shared settings
    # document on first boot; operator edits there become authoritative. The
    # expected guest OS is the VMware ``guestOS`` identifier stored in the
    # datastore VMX, not the Windows marketing name. The base does not need to
    # be registered in ESXi inventory.
    clone_base: str = "ws-2025-base"
    clone_datastore: str = "datastore1"
    clone_guest_os: str = "windows2022srvNext-64"
    clone_network: str = "VM Network"
    clone_max_usage_pct: float = 80.0

    # The built-in local ``Administrator`` password baked into that image before
    # sysprep. Every Windows guest but a domain controller keeps it, so it is the
    # credential the remote-desktop console signs those guests in with — a fact
    # about the image, not a password the platform chooses. Blank means "not
    # recorded": firstboot then resets nothing and no console credential is
    # stored. Seeds ``cloneAdminPasswordEnc`` on the settings document.
    clone_admin_password: str = ""

    # Clone job queue: Valkey is the Celery broker, a per-job pub/sub bus, and the
    # snapshot store the job WebSocket reads from. The clone worker process opens
    # its own ESXi connection against the shared target from the settings document
    # (it can't share the API process's connection object).
    valkey_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str | None = "redis://localhost:6379/2"
    # Per-queue worker concurrency, consumed by the `uv run worker` launcher
    # (app.cli.worker). clone_concurrency caps the esxi queue — the global
    # ceiling on simultaneous clones against the shared ESXi host.
    # provision_concurrency caps the provision queue, whose ops mostly sleep
    # on Valkey pub/sub waiting for guest agents, so it runs a threads pool
    # and can be far higher.
    clone_concurrency: int = 3
    provision_concurrency: int = 16
    # MongoDB — system of record for projects, the VM registry, the settings
    # document, and users. Reachability is checked at startup in the app
    # lifespan (fail-fast ping), not here — a URL default always parses.
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "pki_playground"

    # Executor agent bundling. Both must be set to enable it (a
    # deploy-environment toggle, so env vars like the broker/Mongo config, not
    # the org-wide settings document):
    #   ``EXECUTOR_AGENT_PATH`` — filesystem path on both the API and worker
    #     hosts to the same pki-executor agent binary embedded into each
    #     firstboot ISO. The API hashes it during preflight; the worker bundles it.
    #   ``BACKEND_PUBLIC_URL`` — the browser-facing origin (``http(s)://host:port``);
    #     also the *default* target baked into the agent's executor.toml.
    # If EXECUTOR_AGENT_PATH is unset, the backend uses the repo-bundled
    # backend/agent/pki-executor.exe when present. If no bundled binary is
    # present, the default firstboot ISO carries no agent, so it is safe on
    # golden images whose runner predates the v2 manifest. Per-template
    # provisioning config is NOT baked here — it lives on
    # the VM registry and is dispatched after the agent phones home.
    executor_agent_path: str | None = bundled_executor_agent_path()
    backend_public_url: str | None = None

    # ``AGENT_BACKEND_URL`` — optional override for the origin baked into the
    # agent's executor.toml (the phone-home target), decoupling it from the
    # browser-facing ``BACKEND_PUBLIC_URL``. Guest VMs share the backend's LAN,
    # so pointing agents straight at the LAN backend avoids routing phone-home
    # out to the public (Cloudflare-fronted) FQDN and back — see the phone-home
    # 404 diagnosis. Unset → agents fall back to ``backend_public_url``. Caveat:
    # the agent validates TLS against public webpki-roots, so a private-IP target
    # behind a self-signed cert needs a plain ``http://`` (→ ``ws://``) URL.
    agent_backend_url: str | None = None

    # How long the clone worker waits for a freshly-booted VM's agent to phone
    # home before failing the provision op. Role/feature installs now run as
    # dispatched steps *after* phone-home (not in firstboot), so a healthy VM
    # connects within minutes; this is the safety ceiling for a slow boot.
    agent_phone_home_timeout_s: int = 2700

    # LEGACY-AGENT FALLBACK ONLY: how long the agent connection must stay
    # stable (liveness present, no reconnect) before the worker starts
    # dispatching provisioning, used when the deployed agent predates
    # ``system.boot_info``. New agents are probed actively instead
    # (``agentbus.wait_for_settled_boot``), which is immune to the reconnect
    # churn that resets this dwell. Must exceed AGENT_CONN_TTL_SECONDS (90)
    # plus the intermediate-boot window.
    agent_boot_settle_s: int = 180

    # Admin teardown console — how long a registry entry has to look wrong
    # before it is *flagged* as orphaned. These change what an admin sees
    # classified, never what gets destroyed (that is always an explicit,
    # previewed selection), so they are env knobs rather than settings-document
    # fields: an operator tuning them is tuning a diagnostic, not the platform.
    #   agent_dead — a VM whose agent last phoned home longer ago than this.
    #     Generous by default: a lab left running over a weekend is fine, one
    #     silent for a day is debris.
    #   stuck_cloning — a registry row still in ``cloning`` this long after its
    #     last write; a real clone finishes or errors well inside the hour.
    teardown_agent_dead_after_s: int = 86_400
    teardown_stuck_cloning_after_s: int = 3_600
    # How long an ESXi inventory listing is reused across the polled teardown
    # endpoints. ``list_vm_names`` rebuilds a container view over the whole
    # inventory per call, and the console polls; the destroy preview bypasses
    # this cache, so a stale entry can never reach a confirmation dialog.
    teardown_inventory_cache_s: int = 15

    # Remote desktop. guacd is a local sidecar of the deploy, on the same footing
    # as Mongo and Valkey — so its address is an env var, not a settings-document
    # field: nothing about it is org policy an admin should be editing at runtime,
    # and keeping it here means no new write-only secret to guard. Absent or
    # unreachable degrades exactly one feature; the API starts either way, which
    # is why none of this is in ``_require_secrets``.
    guacd_host: str = "127.0.0.1"
    guacd_port: int = 4822
    # A console ticket is the handoff between the HTTP route that authorizes a
    # session and the WebSocket that opens it. Single-use, and short because the
    # browser opens the socket immediately — a long TTL only widens the window in
    # which a leaked ticket id is worth something.
    console_ticket_ttl_s: int = 60
    # Concurrent relayed sessions. RDP framebuffer traffic shares the API's event
    # loop with every agent socket (single uvicorn worker on purpose — see
    # deploy/prod-deploy.sh), so this is a backstop against one lab's worth of
    # open desktops starving agent dispatch.
    console_max_sessions: int = 8
    # Idle keepalive. An unattended desktop sends nothing, and a reverse proxy
    # will happily reap a silent WebSocket (nginx's proxy_read_timeout defaults
    # to 60s), which reads to the user as a session that died on its own.
    console_keepalive_s: int = 20

    # LEGACY-IMAGE RECOVERY ONLY: uptime (seconds) past which a still-registered
    # FirstBootFinalize scheduled task is treated as having missed its
    # -AtStartup trigger; the worker then dispatches system.reboot. Current
    # single-reboot images do not create this task.
    agent_boot_force_reboot_uptime_s: int = 600

    @property
    def executor_bundling_enabled(self) -> bool:
        return bool(self.executor_agent_path and self.backend_public_url)

    @property
    def effective_agent_backend_url(self) -> str | None:
        """The origin baked into the agent's executor.toml — the explicit
        ``AGENT_BACKEND_URL`` override when set, else ``BACKEND_PUBLIC_URL``."""
        return self.agent_backend_url or self.backend_public_url

    @property
    def oidc_enabled(self) -> bool:
        return bool(
            self.oidc_issuer
            and self.oidc_client_id
            and self.oidc_client_secret
            and self.oidc_redirect_uri
        )

    @model_validator(mode="after")
    def _require_secrets(self) -> "Settings":
        missing = [
            name.upper()
            for name in ("session_secret", "settings_enc_key")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(
                f"Missing required env vars: {', '.join(missing)}. "
                "Generate each with: openssl rand -base64 32"
            )
        return self


settings = Settings()
