/**
 * Live executor-agent presence — which vm_ids currently have a connected
 * agent, pushed over `ws /api/executor/agents/watch` (see `lib/ws.ts`).
 *
 * One socket per authenticated workspace: `Workspace` calls
 * `attachAgentsSocket` on mount and the returned detach on unmount. The
 * backend re-sends the full snapshot on every agent connect/disconnect, so a
 * node's green/offline dot flips the moment presence changes — no polling.
 * On a dropped socket the last snapshot is kept (a transport blip shouldn't
 * flash every dot offline) and the socket reconnects with backoff; the fresh
 * snapshot on reconnect corrects any staleness.
 */

import { create } from "zustand"

import { openAgentsSocket } from "@/lib/ws"

interface AgentsState {
  /** vm_ids with a currently-connected executor agent. */
  onlineVmIds: string[]
  /** Whether the presence socket itself is live — false means `onlineVmIds` may be stale. */
  watching: boolean
}

export const useAgentsStore = create<AgentsState>()(() => ({
  onlineVmIds: [],
  watching: false,
}))

const RECONNECT_DELAYS_MS = [1000, 3000, 10_000]

/**
 * Open (and keep reopening) the presence socket for this session. Returns a
 * detach that stops reconnecting and closes the socket — pair with the
 * workspace's lifetime, not a component render.
 */
export function attachAgentsSocket(
  token: string | null | undefined,
): () => void {
  let closed = false
  let attempt = 0
  let closeSocket: (() => void) | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null

  function open() {
    if (closed) return
    closeSocket = openAgentsSocket(token, {
      onAgents: (vmIds) => {
        attempt = 0 // a snapshot arrived — the connection is genuinely healthy
        useAgentsStore.setState({ onlineVmIds: vmIds, watching: true })
      },
      onClose: () => {
        useAgentsStore.setState({ watching: false })
        if (closed) return
        const delay =
          RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)]
        attempt += 1
        retryTimer = setTimeout(open, delay)
      },
    })
  }

  open()
  return () => {
    closed = true
    if (retryTimer) clearTimeout(retryTimer)
    closeSocket?.()
    useAgentsStore.setState({ onlineVmIds: [], watching: false })
  }
}
