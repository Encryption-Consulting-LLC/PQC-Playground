/**
 * All backend API paths relative to the API base.
 *
 * One entry per backend route the admin app calls — renaming a route is a
 * single-line change here. Mirrors the shape of
 * frontend/src/constants/urls.ts (same convention, admin-relevant subset).
 */

export const API_BASE = "/api"

export const URLS = {
  health: "/health",
  auth: {
    login: "/auth/login",
    logout: "/auth/logout",
    me: "/auth/me",
    config: "/auth/config",
    oidcLogin: "/auth/oidc/login",
    oidcCallback: "/auth/oidc/callback",
  },
  adminUsers: {
    list: "/admin/users",
    create: "/admin/users",
    patch: (username: string) => `/admin/users/${encodeURIComponent(username)}`,
  },
  settings: {
    root: "/settings",
    validateGoldenImage: "/settings/golden-image/validate",
    validateInfrastructure: "/settings/infrastructure/validate",
    validateEnvironment: "/settings/environment/validate",
  },
  ipPool: "/ip-pool",
  // Pre-deployed labs and their join codes. Minting is admin-only
  // (Capability.LAB_ADMIN); the canvas app's `/labs` routes are the redeeming
  // half and are never called from here.
  labs: {
    list: "/admin/labs",
    invites: "/admin/labs/invites",
    invite: (code: string) => `/admin/labs/invites/${encodeURIComponent(code)}`,
  },
  vmRegistry: "/vm-registry",
  deployments: {
    list: "/admin/deployments",
    stop: "/admin/deployments/stop",
  },
  console: {
    // Crosses the ownership boundary an admin otherwise has no way past — the
    // same reason the teardown routes below exist. Gated on VM_CONSOLE_ADMIN,
    // which no operator or guest holds.
    ticket: (vmName: string) => `/admin/console/${encodeURIComponent(vmName)}`,
  },
  teardown: {
    environments: "/admin/teardown/environments",
    orphans: "/admin/teardown/orphans",
    preview: "/admin/teardown/preview",
    start: "/admin/teardown",
    purge: "/admin/teardown/orphans/purge",
  },
  ws: {
    jobs: (jobId: string) => `/ws/jobs/${encodeURIComponent(jobId)}`,
    // Redeems a console ticket and carries the Guacamole protocol. Handed to
    // guacamole-common-js's own tunnel, not `lib/ws.ts`.
    console: (ticketId: string) =>
      `/ws/console/${encodeURIComponent(ticketId)}`,
  },
} as const
