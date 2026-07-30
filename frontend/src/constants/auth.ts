/**
 * Role and capability registries.
 *
 * These are the frontend mirrors of the backend's Role / Capability enums
 * (core/authz.py). String values must stay in sync with the backend.
 *
 * Login is always required — there is no anonymous mode; admins, operators,
 * and guests are all accounts, distinguished by their role. This app is
 * refused to admin accounts (App.tsx) — they belong on the separate `/admin`
 * console, which mirrors the refusal in the other direction.
 *
 * Types are derived from the const objects — there are no hand-written unions.
 * Add or rename a value here and the type updates automatically.
 */

export const ROLES = {
  admin: "admin",
  operator: "operator",
  guest: "guest",
} as const

export type Role = (typeof ROLES)[keyof typeof ROLES]

export const CAPABILITIES = {
  vmList: "vm:list",
  vmRead: "vm:read",
  vmClone: "vm:clone",
  vmUpdate: "vm:update",
  vmPower: "vm:power",
  configGenerate: "config:generate",
  isoAuthor: "iso:author", // operator-only — authored/uploaded config ISOs
  vmExecArbitrary: "vm:exec-arbitrary", // reserved — future executor phase
  deploy: "deploy",
  projectRead: "project:read", // operator-only — gates server-side project persistence
  projectWrite: "project:write",
} as const

export type Capability = (typeof CAPABILITIES)[keyof typeof CAPABILITIES]
