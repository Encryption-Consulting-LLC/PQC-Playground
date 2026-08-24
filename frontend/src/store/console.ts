/**
 * Which remote-desktop session is open, and how it is doing.
 *
 * Deliberately thin. The store holds only serializable facts about the *current*
 * session — no tunnel, no client, no callbacks. Those live in the viewer
 * component's refs, the same separation `store/topology.ts` keeps for its
 * `activeSockets` map: the project autosave subscription walks store state, and a
 * closure or a `WebSocket` reaching it is how you get a serialization crash on
 * an unrelated save.
 *
 * One session at a time. The overlay is full-screen, so a second concurrent
 * session would have nowhere to render, and the backend caps concurrency anyway.
 * Opening a new one replaces the old — `open()` is also the switch action.
 */

import { create } from "zustand"

import { ApiError, mintConsoleTicket } from "@/lib/api"

/** Lifecycle of the viewer, which is *not* the tunnel's own state enum.
 *
 * `authorizing` covers the ticket POST, which is where every actionable refusal
 * comes from (no credentials → redeploy, still cloning, guacd down). `connecting`
 * is the tunnel handshake. Keeping them apart is what lets the overlay say which
 * half failed instead of "could not connect". */
export type ConsoleStatus =
  | "idle"
  | "authorizing"
  | "connecting"
  | "connected"
  | "error"

export interface ConsoleSession {
  /** Canvas node id — what the overlay titles itself from. */
  nodeId: string
  /** Real ESXi inventory name; the ticket is minted against this. */
  vmName: string
  /** Display label for the node, so the overlay does not re-read the topology. */
  label: string
  /** Who the session signs in as, e.g. `ENCON\Administrator`. Display only. */
  credentialLabel?: string
  /** The single-use ticket the viewer dials with. Set once authorization
   * succeeds; a reconnect mints a new one rather than reusing this. Safe to keep
   * in store state because this store is not persisted — no `persist` middleware,
   * and the project autosave serializer walks the topology store, not this one. */
  ticketId?: string
  status: ConsoleStatus
  /** Populated on `error` — already user-facing, no further mapping needed. */
  detail?: string
  /** Bumped to force the viewer to remount and redial. A ticket is single-use,
   * so a reconnect is a fresh authorization, not a socket retry. */
  attempt: number
}

interface ConsoleState {
  session: ConsoleSession | null
  /** Authorize and open a session on `node`, replacing any current one. */
  start: (node: {
    nodeId: string
    vmName: string
    label: string
  }) => Promise<void>
  setStatus: (status: ConsoleStatus, detail?: string) => void
  /** Mint a fresh ticket for the current session and dial again. */
  reconnect: () => Promise<void>
  close: () => void
}

export const useConsoleStore = create<ConsoleState>()((set, get) => {
  /** The authorization round trip, shared by `start` and `reconnect` so the
   * overlay's Reconnect and the Inspector's Connect cannot diverge — the earlier
   * split left Reconnect setting a status nothing acted on, and the overlay spun
   * forever. */
  async function authorize(session: ConsoleSession) {
    set({ session })
    try {
      const ticket = await mintConsoleTicket(session.vmName)
      set((s) =>
        // Guarded: the user may have closed the overlay, or started another
        // session, while this request was in flight. Either way this ticket is
        // stale and must not resurrect or hijack the visible session.
        s.session?.nodeId === session.nodeId &&
        s.session.attempt === session.attempt
          ? {
              session: {
                ...s.session,
                ticketId: ticket.ticketId,
                credentialLabel: ticket.credentialLabel,
                status: "connecting",
              },
            }
          : s,
      )
    } catch (error) {
      const detail =
        error instanceof ApiError
          ? error.message
          : "Could not start a remote desktop session."
      set((s) =>
        s.session?.nodeId === session.nodeId &&
        s.session.attempt === session.attempt
          ? { session: { ...s.session, status: "error", detail } }
          : s,
      )
    }
  }

  return {
    session: null,

    start: ({ nodeId, vmName, label }) =>
      authorize({ nodeId, vmName, label, status: "authorizing", attempt: 0 }),

    setStatus: (status, detail) =>
      set((s) =>
        s.session ? { session: { ...s.session, status, detail } } : s,
      ),

    reconnect: async () => {
      const current = get().session
      if (!current) return
      await authorize({
        ...current,
        status: "authorizing",
        detail: undefined,
        // Dropped, so a spent ticket can never be redialled.
        ticketId: undefined,
        attempt: current.attempt + 1,
      })
    },

    close: () => set({ session: null }),
  }
})
