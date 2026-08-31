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
  // Raw PowerShell through the executor agent. Held by operators *and*
  // guests: the command route resolves the agent back to its registry row and
  // applies the same own-namespace check as remote desktop, so the reach is a
  // shell on a VM the holder already deploys and destroys.
  vmExecArbitrary: "vm:exec-arbitrary",
  // Remote desktop. Held by operators *and* guests — a session only reaches a
  // VM the holder could already deploy and destroy, and the backend scopes it
  // to the caller's own VMs. Admins hold the separate vm:console-admin instead
  // and use the /admin console, which this app never renders.
  vmConsole: "vm:console",
  // Redeem an admin-issued join code for a pre-deployed lab. Held by guests
  // and operators; the issuing side (lab:admin) is admin-only and lives in the
  // /admin console, which this app never renders.
  labJoin: "lab:join",
  deploy: "deploy",
  // Held by every canvas role; the backend scopes each project to its owner,
  // so these gate whether an account has saved projects, never whose.
  projectRead: "project:read",
  projectWrite: "project:write",
} as const

export type Capability = (typeof CAPABILITIES)[keyof typeof CAPABILITIES]
