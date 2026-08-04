/**
 * The one in-flight teardown job, persisted across reloads.
 *
 * A reload returns to the same URL now that the console is routed, but the
 * job id is the only handle on the progress stream, and it lives nowhere else
 * — without persistence a teardown started a moment earlier would still
 * vanish from the UI while the worker kept destroying VMs.
 *
 * Only the job's *identity* is stored: id, when it started, a human label, and
 * the targets. Op state is deliberately never persisted, because the backend
 * replays the last `plan-state` snapshot on connect and that message is
 * whole-state by design — reconnecting rebuilds live progress for free, and a
 * stale bar restored from localStorage is impossible.
 *
 * The storage key is deliberately not shared with the operator app: this is an
 * admin-initiated cross-user action the canvas has no business resuming.
 */

import { create } from "zustand"
import { persist } from "zustand/middleware"

import { STORAGE_KEYS } from "@/constants"

export interface TeardownJob {
  jobId: string
  startedAt: number
  /** What the admin chose, e.g. "acme-pki" or a VM name — used in the card. */
  label: string
  vmNames: string[]
}

interface TeardownState {
  job: TeardownJob | null
  start: (job: TeardownJob) => void
  clear: () => void
}

export const useTeardownStore = create<TeardownState>()(
  persist(
    (set) => ({
      job: null,
      start: (job) => set({ job }),
      clear: () => set({ job: null }),
    }),
    { name: STORAGE_KEYS.teardown },
  ),
)
