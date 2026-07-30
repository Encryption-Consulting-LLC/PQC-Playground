/**
 * Generic client for the backend's job-progress WebSocket.
 *
 * Mirrors the backend `ProgressMessage` union (app/core/jobs/models.py). Opening
 * a socket subscribes to one job's stream; the returned `close()` tears it down.
 * The URL is built from the current origin + API base so the Vite dev proxy
 * forwards the upgrade (see vite.config.ts `ws: true`).
 */

import { API_BASE, URLS } from "@/constants"

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

export type JobMessage =
  | QueuedEvent
  | RunningEvent
  | ProgressEvent
  | PlanStateEvent
  | DoneEvent
  | ErrorEvent

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

  // A socket that closes without a terminal frame (backend crash, dropped
  // connection) is a failure the caller needs to react to.
  ws.onclose = () => {
    if (!settled) {
      settled = true
      handlers.onError?.({
        type: "error",
        status: 0,
        detail: "Progress connection closed before completion.",
      })
    }
  }

  return () => {
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
