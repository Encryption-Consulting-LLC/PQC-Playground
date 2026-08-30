/**
 * Generic client for the backend's job-progress WebSocket.
 *
 * Mirrors the backend `ProgressMessage` union (app/core/jobs/models.py). Opening
 * a socket subscribes to one job's stream; the returned `close()` tears it down.
 * The URL is built from the current origin + API base so the Vite dev proxy
 * forwards the upgrade (see vite.config.ts `ws: true`).
 */

import { API_BASE, URLS } from "@/constants"

/** The job's snapshot expired — reconnecting will never succeed. */
export const WS_CLOSE_JOB_GONE = 4404
/** The session token was rejected on the upgrade. */
export const WS_CLOSE_UNAUTHORIZED = 4401
/** Console only: guacd is up but this session cannot start (service down, or
 * every session slot in use). Retryable, unlike 4404. */
export const WS_CLOSE_CONSOLE_UNAVAILABLE = 4503
/** Console only: the VM is being torn down — the same code the guest agent's
 * socket gets, so "this VM is going away" means one thing everywhere. */
export const WS_CLOSE_VM_GONE = 4410

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

export interface DoneEvent {
  type: "done"
  result: Record<string, unknown>
}

export interface ErrorEvent {
  type: "error"
  status: number
  detail: string
  /** Close code, on a transport failure (`status: 0`) only — 4401/4404 above. */
  code?: number
}

/** Current run state of one op within a deploy plan — mirrors the backend's `OpRunState`. */
export interface OpRunState {
  status: "pending" | "queued" | "running" | "done" | "error" | "cancelled"
  percent?: number
  phase?: string
  detail?: string
  /** Python traceback for unexpected failures — collapsible technical detail. */
  trace?: string
  result?: Record<string, unknown>
  steps?: Record<string, StepRunState>
}

export interface StepRunState {
  status: "pending" | "queued" | "running" | "done" | "error" | "cancelled"
  percent?: number
  phase?: string
  detail?: string
}

/** Full snapshot of every op's state in a deploy plan, published whole on every transition. */
export interface PlanStateEvent {
  type: "plan-state"
  ops: Record<string, OpRunState>
}

/**
 * Server-sent idle heartbeat (backend `ws.py::_HEARTBEAT`). Carries nothing —
 * its only job is to arrive, proving the socket is still whole and re-arming
 * the silence watchdog below.
 */
export interface PingEvent {
  type: "ping"
}

export type JobMessage =
  | QueuedEvent
  | RunningEvent
  | ProgressEvent
  | PlanStateEvent
  | PingEvent
  | DoneEvent
  | ErrorEvent

/**
 * How long the job socket may say nothing before we treat it as dead.
 *
 * The backend heartbeats every `job_keepalive_s` (20s), so three missed beats
 * is a real failure rather than jitter. This watchdog is the half of the fix
 * that recovers rather than prevents: a WebSocket reaped by a proxy does not
 * reliably surface as a `close` event, and `onclose` was the only thing this
 * client ever reacted to — so a silently dropped socket produced no error, no
 * reconnect, and a deploy that had already finished server-side sat "deploying"
 * on the canvas for 8h31m until the page was reloaded.
 */
const JOB_SOCKET_SILENCE_MS = 60_000

export interface JobSocketHandlers {
  /** Job accepted but waiting on the worker pool's concurrency cap. */
  onQueued?: (event: QueuedEvent) => void
  /** Job picked up by a worker; work is about to start. */
  onRunning?: (event: RunningEvent) => void
  onProgress?: (event: ProgressEvent) => void
  /** Full deploy-plan op snapshot — non-terminal, arrives once per op transition. */
  onPlanState?: (event: PlanStateEvent) => void
  onDone?: (event: DoneEvent) => void
  /** Backend `error` frame, or a transport failure (status 0). */
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
 * The console cannot use `openJobSocket`: guacamole-common-js brings its own
 * `WebSocketTunnel` and speaks the Guacamole protocol, not this module's
 * `JobMessage` union. It shares the `?token=` and `WS_CLOSE_*` conventions and
 * nothing else.
 *
 * The token is returned separately because `WebSocketTunnel.connect(data)`
 * builds the socket as `` `${tunnelURL}?${data}` `` itself. Appending the query
 * here too would produce `…?token=x?token=x` and a 4401 on a token the caller
 * did in fact have.
 */
export function consoleTunnelUrl(ticketId: string): string {
  return wsUrl(URLS.ws.console(ticketId), null)
}

/** The `data` argument for `WebSocketTunnel.connect` — see `consoleTunnelUrl`. */
export function consoleTunnelData(token: string | null | undefined): string {
  return token ? `token=${encodeURIComponent(token)}` : ""
}

/**
 * Subscribe to a job's progress. Returns a `close()` that detaches handlers and
 * closes the socket; safe to call once the job has reached a terminal state.
 */
export function openJobSocket(
  jobId: string,
  token: string | null | undefined,
  handlers: JobSocketHandlers,
): () => void {
  const ws = new WebSocket(wsUrl(URLS.ws.jobs(jobId), token))
  let settled = false
  let watchdog: ReturnType<typeof setTimeout> | undefined

  // Reported as a transport failure (`status: 0`) with the generic abnormal
  // close code, because that is exactly what it is — the caller's existing
  // retry ladder reconnects, and the reconnect replays the snapshot (or the
  // persisted plan run), which is what finally delivers the terminal frame.
  const onSilence = () => {
    if (settled) return
    settled = true
    ws.onmessage = null
    ws.onclose = null
    ws.close()
    handlers.onError?.({
      type: "error",
      status: 0,
      detail: "Progress connection went silent — reconnecting.",
      code: 1006,
    })
  }

  const armWatchdog = () => {
    clearTimeout(watchdog)
    if (!settled) watchdog = setTimeout(onSilence, JOB_SOCKET_SILENCE_MS)
  }

  armWatchdog()

  ws.onmessage = (ev) => {
    armWatchdog()
    let msg: JobMessage
    try {
      msg = JSON.parse(ev.data) as JobMessage
    } catch {
      return
    }
    switch (msg.type) {
      case "ping":
        // Nothing to do — arriving was the point.
        break
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
        clearTimeout(watchdog)
        handlers.onDone?.(msg)
        break
      case "error":
        settled = true
        clearTimeout(watchdog)
        handlers.onError?.(msg)
        break
    }
  }

  // A socket that closes without a terminal frame (backend crash, dropped
  // connection) is a failure the caller needs to react to. The close code
  // rides along: a rejected token (4401) or an expired job (4404) will never
  // recover, and retrying them as if they were a blip hides the real cause.
  ws.onclose = (ev) => {
    clearTimeout(watchdog)
    if (!settled) {
      settled = true
      handlers.onError?.({
        type: "error",
        status: 0,
        detail: "Progress connection closed before completion.",
        code: ev.code,
      })
    }
  }

  return () => {
    clearTimeout(watchdog)
    ws.onmessage = null
    ws.onclose = null
    ws.close()
  }
}

/** One agent-presence snapshot from `ws /api/executor/agents/watch` — the full set of connected vm_ids, re-sent whole on every change. */
export interface AgentsEvent {
  type: "agents"
  vm_ids: string[]
}

/**
 * Subscribe to live executor-agent presence. `onAgents` fires with a full
 * snapshot on connect and again the moment any agent connects or disconnects;
 * `onClose` fires when the socket drops for any reason (the caller owns
 * reconnect policy). Returns a `close()` that detaches handlers silently.
 */
export function openAgentsSocket(
  token: string | null | undefined,
  handlers: { onAgents: (vmIds: string[]) => void; onClose?: () => void },
): () => void {
  const ws = new WebSocket(wsUrl(URLS.ws.agents, token))

  ws.onmessage = (ev) => {
    let msg: AgentsEvent
    try {
      msg = JSON.parse(ev.data) as AgentsEvent
    } catch {
      return
    }
    if (msg.type === "agents" && Array.isArray(msg.vm_ids)) {
      handlers.onAgents(msg.vm_ids)
    }
  }

  ws.onclose = () => {
    handlers.onClose?.()
  }

  return () => {
    ws.onmessage = null
    ws.onclose = null
    ws.close()
  }
}
