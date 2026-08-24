/**
 * Client for the backend's job-progress WebSocket — the admin app's only one.
 *
 * Mirrors the backend message union (app/core/jobs/models.py). Opening a socket
 * subscribes to one job's stream; the returned `close()` tears it down. The URL
 * is built from the current origin plus the API base, so the Vite dev proxy
 * forwards the upgrade (`ws: true` in vite.config.ts) and production works
 * same-origin unchanged.
 *
 * Two deliberate differences from the operator app's equivalent:
 *
 *   - The close code is passed through on the synthesized error. The backend
 *     closes 4404 when a job's Valkey snapshot has expired and 4401 on a bad
 *     token; without the code, a job that has simply aged out is
 *     indistinguishable from a dropped connection and gets retried forever.
 *   - The session token is read from the store rather than passed in, matching
 *     `api.ts`'s `request()` convention.
 */

import { API_BASE, URLS } from "@/constants"
import { useAuthStore } from "@/store/auth"

/** The job's snapshot expired — reconnecting will never succeed. */
export const WS_CLOSE_JOB_GONE = 4404
/** The session token was rejected on the upgrade. */
export const WS_CLOSE_UNAUTHORIZED = 4401

export interface QueuedEvent {
  type: "queued"
}

export interface RunningEvent {
  type: "running"
}

export interface ProgressEvent {
  type: "progress"
  percent: number
  phase: string
  key: string
  unit: string
}

/** Run state of one target within a job — mirrors the backend's `OpRunState`. */
export interface OpRunState {
  status: "pending" | "queued" | "running" | "done" | "error" | "cancelled"
  percent?: number
  phase?: string
  detail?: string
  /** Python traceback for unexpected failures — rendered as collapsible detail. */
  trace?: string
  result?: Record<string, unknown>
}

/** Full snapshot of every target's state, published whole on every transition. */
export interface PlanStateEvent {
  type: "plan-state"
  ops: Record<string, OpRunState>
}

export interface DoneEvent {
  type: "done"
  result: Record<string, unknown>
}

export interface ErrorEvent {
  type: "error"
  status: number
  detail: string
  /** WebSocket close code, present only on a synthesized transport error. */
  code?: number
}

export type JobMessage =
  | QueuedEvent
  | RunningEvent
  | ProgressEvent
  | PlanStateEvent
  | DoneEvent
  | ErrorEvent

export interface JobSocketHandlers {
  onQueued?: (event: QueuedEvent) => void
  onRunning?: (event: RunningEvent) => void
  onProgress?: (event: ProgressEvent) => void
  onPlanState?: (event: PlanStateEvent) => void
  onDone?: (event: DoneEvent) => void
  /** A backend `error` frame, or a transport failure (status 0, `code` set). */
  onError?: (event: ErrorEvent) => void
}

function wsUrl(path: string, token: string | null | undefined): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  const base = `${proto}//${window.location.host}${API_BASE}${path}`
  return token ? `${base}?token=${encodeURIComponent(token)}` : base
}

/**
 * The console tunnel's URL — deliberately *without* a query string.
 *
 * guacamole-common-js brings its own `WebSocketTunnel` and speaks the Guacamole
 * protocol, so it cannot use `openJobSocket`; it shares the `?token=` convention
 * and nothing else. The token is returned separately because
 * `WebSocketTunnel.connect(data)` builds the socket as `` `${url}?${data}` ``
 * itself — appending the query here too yields `…?token=x?token=x` and a 4401 on
 * a token the caller did in fact have.
 */
export function consoleTunnelUrl(ticketId: string): string {
  return wsUrl(URLS.ws.console(ticketId), null)
}

/** The `data` argument for `WebSocketTunnel.connect` — see `consoleTunnelUrl`. */
export function consoleTunnelData(): string {
  const token = useAuthStore.getState().token
  return token ? `token=${encodeURIComponent(token)}` : ""
}

/**
 * Subscribe to a job's progress. Returns a `close()` that detaches handlers and
 * closes the socket; safe to call after a terminal frame.
 *
 * The server replays the last snapshot on connect, so attaching to a job that
 * is already running rebuilds full state with no extra request.
 */
export function openJobSocket(jobId: string, handlers: JobSocketHandlers): () => void {
  const ws = new WebSocket(wsUrl(URLS.ws.jobs(jobId), useAuthStore.getState().token))
  let settled = false

  ws.onmessage = (ev) => {
    let msg: JobMessage
    try {
      msg = JSON.parse(ev.data) as JobMessage
    } catch {
      return
    }
    switch (msg.type) {
      case "queued":
        handlers.onQueued?.(msg)
        break
      case "running":
        handlers.onRunning?.(msg)
        break
      case "progress":
        handlers.onProgress?.(msg)
        break
      case "plan-state":
        handlers.onPlanState?.(msg)
        break
      case "done":
        settled = true
        handlers.onDone?.(msg)
        break
      case "error":
        settled = true
        handlers.onError?.(msg)
        break
    }
  }

  // A socket that closes without a terminal frame is a failure the caller has
  // to react to — and the code is how it tells "expired" from "dropped".
  ws.onclose = (ev) => {
    if (settled) return
    settled = true
    handlers.onError?.({
      type: "error",
      status: 0,
      detail: "Progress connection closed before completion.",
      code: ev.code,
    })
  }

  return () => {
    ws.onmessage = null
    ws.onclose = null
    ws.close()
  }
}
