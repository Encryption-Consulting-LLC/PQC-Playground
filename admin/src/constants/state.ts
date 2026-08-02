/**
 * Client-side state keys.
 *
 * STORAGE_KEYS — zustand persist / localStorage keys. Deliberately the SAME
 * values as frontend/src/constants/state.ts: both apps are served
 * same-origin, so a session or theme choice made in one app is honored in
 * the other with no cross-app sync code.
 * QUERY_KEYS   — TanStack Query cache keys; keep them here so every
 *                invalidation call uses the same reference.
 */

export const STORAGE_KEYS = {
  auth: "ec-pki-auth",
  theme: "ec-pki-theme",
  // Deliberately NOT shared with the operator app: this tracks an
  // admin-initiated cross-user teardown, which the canvas has no business
  // resuming. Only the job id is kept — op state is replayed by the server.
  teardown: "ec-pki-admin-teardown",
} as const

export const QUERY_KEYS = {
  config: ["auth-config"] as const,
  me: ["auth-me"] as const,
  users: ["admin-users"] as const,
  settings: ["admin-settings"] as const,
  ipPool: ["admin-ip-pool"] as const,
  registry: ["admin-vm-registry"] as const,
  deployments: ["admin-deployments"] as const,
  environments: ["admin-environments"] as const,
  orphans: ["admin-orphans"] as const,
  teardownPreview: ["admin-teardown-preview"] as const,
} as const
