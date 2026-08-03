/**
 * Persistence mode switch — localStorage vs. server (Mongo-backed projects API).
 *
 * Deliberately import-free (aside from a zustand type) so it can sit under
 * `store/projects.ts` without creating cycles.
 *
 * Two independent switches live here:
 *
 * **Mode.** `initServerProjects()` (lib/projectSync.ts) flips server mode before
 * any store write can happen, which turns the persist middleware's storage into
 * a read-only passthrough — the server is then the record and localStorage must
 * not shadow it.
 *
 * **Account scope.** Every browser-local key is suffixed with the signed-in
 * username. One shared key was how a project belonging to one account showed up
 * for the next person to sign in on that browser: the store hydrates from
 * localStorage at import time, long before anyone has authenticated. Until a
 * scope is set the storage is therefore *inert* — reads return null and writes
 * are dropped — so the pre-login store can only ever be empty, and each account
 * reads and writes its own drawer.
 */

import type { StateStorage } from "zustand/middleware"

let server = false
let accountScope: string | null = null

export function enableServerPersistence() {
  server = true
}

/** Return to local persistence when the authenticated session changes. */
export function disableServerPersistence() {
  server = false
}

export function isServerPersistence() {
  return server
}

/**
 * Bind browser-local storage to one account, or to none.
 *
 * Called on every session change (including sign-out, with null) *before* the
 * project set is replaced, so no write can land in the outgoing account's
 * drawer — or, when null, in any drawer at all.
 */
export function setAccountScope(username: string | null) {
  accountScope = username
}

export function getAccountScope() {
  return accountScope
}

/**
 * The account's key for a logical storage name, or null when no account is
 * bound. Exported because `lib/projectSync.ts` reads two of these keys directly
 * rather than through the persist middleware, and both must agree on the name.
 */
export function scopedKey(name: string): string | null {
  return accountScope === null ? null : `${name}:${accountScope}`
}

/**
 * Per-account localStorage passthrough that goes read-only in server mode, and
 * is inert entirely until an account is bound.
 */
export const gatedLocalStorage: StateStorage = {
  getItem: (name) => {
    const key = scopedKey(name)
    return key === null ? null : localStorage.getItem(key)
  },
  setItem: (name, value) => {
    const key = scopedKey(name)
    if (key !== null && !server) localStorage.setItem(key, value)
  },
  removeItem: (name) => {
    const key = scopedKey(name)
    if (key !== null && !server) localStorage.removeItem(key)
  },
}
