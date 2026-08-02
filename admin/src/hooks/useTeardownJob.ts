/**
 * Live progress for the current teardown job.
 *
 * Attaches to `/api/ws/jobs/{jobId}` for whichever job the teardown store
 * holds — including one restored from localStorage after a reload, since the
 * backend replays the last snapshot on connect.
 *
 * Behaviour on a dropped socket is the one non-obvious part. Unlike the
 * operator app's deploy stream there is no optimistic state to unwind here:
 * the tables are polled and converge on their own, and the worker is almost
 * certainly still destroying VMs. So a transport drop retries and, on
 * exhaustion, warns — it never clears the job or reverts anything. Only close
 * code 4404 (the job's snapshot expired, so reconnecting can never succeed)
 * clears it.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { QUERY_KEYS } from "@/constants"
import { plural } from "@/lib/display"
import {
  openJobSocket,
  WS_CLOSE_JOB_GONE,
  WS_CLOSE_UNAUTHORIZED,
  type OpRunState,
} from "@/lib/ws"
import { useTeardownStore } from "@/store/teardown"

/** Backoff ladder for a dropped transport, in milliseconds. */
const RETRY_DELAYS = [500, 1500, 3000]

export type TeardownPhase = "queued" | "running" | "done" | "error"

export interface TeardownJobState {
  phase: TeardownPhase
  /** Whole-state, replaced on every `plan-state` frame — never merged. */
  ops: Record<string, OpRunState>
  /** Set when the stream dropped but the job is presumed still running. */
  warning: string | null
  /** Terminal failure detail, when the job itself errored. */
  error: string | null
}

const INITIAL: TeardownJobState = { phase: "queued", ops: {}, warning: null, error: null }

export function useTeardownJob() {
  const job = useTeardownStore((s) => s.job)
  const clearJob = useTeardownStore((s) => s.clear)
  const queryClient = useQueryClient()

  const [state, setState] = useState<TeardownJobState>(INITIAL)
  const retryRef = useRef(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Reset during render rather than in an effect (React's "adjusting state
  // when props change" pattern): resetting in the effect would render one
  // frame of the previous job's progress under the new job's heading.
  const seenJobRef = useRef<string | null>(null)
  if (seenJobRef.current !== (job?.jobId ?? null)) {
    seenJobRef.current = job?.jobId ?? null
    setState(INITIAL)
  }

  const invalidate = useCallback(() => {
    // A destroyed VM leaves all four of these stale at once.
    for (const key of [
      QUERY_KEYS.environments,
      QUERY_KEYS.orphans,
      QUERY_KEYS.registry,
      QUERY_KEYS.ipPool,
    ]) {
      queryClient.invalidateQueries({ queryKey: key })
    }
  }, [queryClient])

  useEffect(() => {
    if (!job) return

    let closed = false
    let detach: (() => void) | null = null
    retryRef.current = 0

    const attach = () => {
      if (closed) return
      detach = openJobSocket(job.jobId, {
        onRunning: () => setState((s) => ({ ...s, phase: "running", warning: null })),
        onPlanState: (msg) => {
          retryRef.current = 0
          setState((s) => ({ ...s, phase: "running", ops: msg.ops, warning: null }))
          if (Object.values(msg.ops).some((op) => op.status === "done")) invalidate()
        },
        onDone: (msg) => {
          const destroyed = (msg.result.destroyed as string[] | undefined) ?? []
          const absent = (msg.result.alreadyAbsent as string[] | undefined) ?? []
          const failed = (msg.result.failed as string[] | undefined) ?? []
          setState((s) => ({ ...s, phase: "done", warning: null }))
          invalidate()
          // Toasted as well as shown on the card: a fully torn-down lab
          // disappears from the table, so the card is the only other place
          // the outcome exists — and it can be dismissed.
          const parts = [`${plural(destroyed.length, "VM")} destroyed`]
          if (absent.length) parts.push(`${absent.length} already gone`)
          if (failed.length) parts.push(`${failed.length} failed`)
          if (failed.length) toast.warning(parts.join(", "))
          else toast.success(parts.join(", "))
        },
        onError: (msg) => {
          if (msg.status !== 0) {
            setState((s) => ({ ...s, phase: "error", error: msg.detail }))
            invalidate()
            return
          }
          // The token was rejected; the next HTTP call's 401 handler logs out.
          if (msg.code === WS_CLOSE_UNAUTHORIZED) return
          // The job aged out of Valkey — reconnecting can never succeed.
          if (msg.code === WS_CLOSE_JOB_GONE) {
            clearJob()
            toast("That teardown's progress stream has expired.")
            return
          }
          const delay = RETRY_DELAYS[retryRef.current]
          if (delay === undefined) {
            setState((s) => ({
              ...s,
              warning:
                "Lost the progress stream. The teardown is still running on the server — the tables below will catch up.",
            }))
            invalidate()
            return
          }
          retryRef.current += 1
          timerRef.current = setTimeout(attach, delay)
        },
      })
    }

    attach()
    return () => {
      closed = true
      detach?.()
      // Both, always: StrictMode double-invokes effects, and a surviving timer
      // would start a second retry ladder against the same job.
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
  }, [job, clearJob, invalidate])

  return { job, state, dismiss: clearJob }
}
